"""Log in by driving Eitaa's own login FORM, the way a person does.

Why this exists
---------------
The API login worked right up to the last step and then threw the session away.
Measured on the live server after a sign-in that Eitaa itself confirmed by
notification:

    authState            : "authStateSignIn"     (still on the sign-in step)
    rootScope.getMyId()  : 0
    appUsersManager.getSelf() : null
    contacts.getContacts : INVALID_LOGIN
    page                 : back to "ورود به ایتا"

The reason is structural, not a bug to patch. Calling `auth.signIn` directly
authorizes the connection, but the thing that PERSISTS a session - writing the
user, the dc and the auth marker into the profile - is Eitaa's own login code,
which we bypassed. `apiManagerProxy.dispatchUserAuth()` cannot rescue it: it
only READS state the app already stored, so with nothing stored it is a no-op.
On the previous build `setUserAuth` did the storing and that is why the old
approach worked at all; that method no longer exists.

Driving the form hands the whole problem back to Eitaa. It stores the session
exactly as it does for a human, which also means this path does not break the
next time Eitaa rearranges its internals - the form is a phone field and a
button, and that does not change.

What the live page looks like (recorded 2026-10-11):

    div.input-field.input-select      > div.input-field-input[contenteditable]  "ایران"
    div.input-field.input-field-phone > div.input-field-input[contenteditable]  "+98"
    button.btn-primary                                                          "ادامه"

tweb names its login steps `.page-sign` (phone), `.page-authCode` (code),
`.page-password` (2FA), `.page-signUp` (not registered). Those class names are
stable across builds and are what this module keys on.

SAFETY: request_code() submits the form exactly ONCE. Nothing here loops or
retries - repeatedly asking for a code is what gets a number rate-limited. A
FLOOD message from the page is returned verbatim so the caller can stop.
"""

from __future__ import annotations

import re
import time
from typing import Any

# Each element has several candidates: the first that is visible wins, so a
# class rename does not take the login down. The contenteditable div is the
# current build; the plain input is the older one.
COUNTRY_FIELD = [
    ".input-field.input-select div.input-field-input[contenteditable='true']",
    ".input-select [contenteditable='true']",
    "input.input-field-input.input-select",
]

PHONE_FIELD = [
    ".input-field-phone div.input-field-input[contenteditable='true']",
    "div.input-field-phone [contenteditable='true']",
    "#auth-pages .input-field-phone [contenteditable='true']",
    "input.input-field-input[inputmode='decimal']",
    "input[type='tel']",
]

CONTINUE_BUTTON = [
    ".page-sign button.btn-primary",
    "#auth-pages button.btn-primary",
    ".input-wrapper button.btn-primary",
    "button.btn-primary",
]

CODE_FIELD = [
    ".page-authCode div.input-field-input[contenteditable='true']",
    ".page-authCode [contenteditable='true']",
    ".page-authCode input.input-field-input",
    "#auth-pages .input-field-input[contenteditable='true']",
]

# Which step of the login the page is showing.
PAGE_PHONE = ".page-sign"
PAGE_CODE = ".page-authCode"
PAGE_PASSWORD = ".page-password"
PAGE_SIGNUP = ".page-signUp"

# Error/toast text the page shows for a refused phone or code.
_ERROR_SELECTORS = [
    ".input-field.error .input-field-name",
    ".input-wrapper .error",
    ".toast",
    ".popup-body",
]


async def _visible(page: Any, selectors: list[str], timeout_ms: int = 8000):
    """First visible locator among candidates, or None. Never raises."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible():
                    return loc, sel
            except Exception:  # noqa: BLE001
                continue
        try:
            await page.wait_for_timeout(200)
        except Exception:  # noqa: BLE001
            return None, None
    return None, None


async def _has(page: Any, selector: str) -> bool:
    try:
        return await page.evaluate(
            "(s) => { try { return !!document.querySelector(s); } "
            "catch (e) { return false; } }", selector)
    except Exception:  # noqa: BLE001
        return False


async def current_step(page: Any) -> str:
    """Which login step is on screen: phone / code / password / signup / none."""
    for name, sel in (("code", PAGE_CODE), ("password", PAGE_PASSWORD),
                      ("signup", PAGE_SIGNUP), ("phone", PAGE_PHONE)):
        if not await _has(page, sel):
            continue
        # tweb keeps every step in the DOM and shows one, so visibility decides.
        try:
            if await page.locator(sel).first.is_visible():
                return name
        except Exception:  # noqa: BLE001
            continue
    return "none"


async def page_error(page: Any) -> str | None:
    """Any error text the form is showing (wrong code, flood, invalid phone)."""
    for sel in _ERROR_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                txt = (await loc.inner_text()).strip()
                if txt:
                    return txt[:200]
        except Exception:  # noqa: BLE001
            continue
    return None


def national_part(intl: str) -> str:
    """Digits to type after the country code the field already shows.

    `989104938987` -> `9104938987`. The phone field is pre-filled with "+98"
    from the country selector, so typing the country code again produces
    "+98989...", which the page rejects as an invalid number.
    """
    s = re.sub(r"\D", "", intl or "")
    return s[2:] if s.startswith("98") and len(s) > 10 else s


async def _type_into(page: Any, loc: Any, text: str) -> str:
    """Click, put the caret at the end, type. Returns what the field shows."""
    await loc.click(timeout=8000)
    try:
        # A contenteditable div keeps the "+98" prefix; typing must append to it,
        # not replace it, so the caret goes to the end first.
        await page.keyboard.press("End")
    except Exception:  # noqa: BLE001
        pass
    await loc.type(text, delay=70, timeout=20000)
    try:
        return (await loc.inner_text()).strip()
    except Exception:  # noqa: BLE001
        try:
            return str(await loc.input_value())
        except Exception:  # noqa: BLE001
            return ""


async def request_code(driver: Any, intl_phone: str,
                       wait_s: float = 45.0) -> dict:
    """Fill the phone in and press continue, ONCE.

    Returns {ok, step, typed, detail}. `step` is where the page ended up:
    "code" is success, "password" means 2FA before any code, "signup" means the
    number has no Eitaa account, "phone" means the form refused it.
    """
    page = driver.page
    step = await current_step(page)
    if step == "none":
        return {"ok": False, "code": "the login form is not on screen "
                                     "(already logged in, or the page did not load)"}
    if step != "phone":
        # A half-finished attempt from a previous run: reload to get a clean form.
        print(f"[login-ui] page was on the '{step}' step; reloading to start clean",
              flush=True)
        try:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
        except Exception:  # noqa: BLE001
            pass

    field, sel = await _visible(page, PHONE_FIELD, timeout_ms=15000)
    if field is None:
        return {"ok": False, "code": "no phone field found on the login form"}

    digits = national_part(intl_phone)
    if not digits:
        return {"ok": False, "code": f"nothing to type for phone {intl_phone!r}"}
    shown = await _type_into(page, field, digits)
    print(f"[login-ui] typed into {sel}; field now reads {shown!r}", flush=True)
    typed_digits = re.sub(r"\D", "", shown)
    if digits not in typed_digits:
        return {"ok": False, "code": f"the field did not accept the number "
                                     f"(reads {shown!r})", "typed": shown}

    button, bsel = await _visible(page, CONTINUE_BUTTON, timeout_ms=8000)
    if button is None:
        return {"ok": False, "code": "no continue button on the login form",
                "typed": shown}
    try:
        if await button.is_disabled():
            return {"ok": False, "code": "the continue button stayed disabled, so "
                                         "the page considers the number invalid",
                    "typed": shown}
    except Exception:  # noqa: BLE001
        pass

    # THE one submit. Everything below only observes.
    print(f"[login-ui] clicking {bsel} - this requests ONE code", flush=True)
    try:
        await button.click(timeout=10000)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"could not click continue: {exc}",
                "typed": shown}

    deadline = time.time() + wait_s
    while time.time() < deadline:
        step = await current_step(page)
        if step in ("code", "password", "signup"):
            print(f"[login-ui] page moved to the '{step}' step after "
                  f"{wait_s - (deadline - time.time()):.1f}s", flush=True)
            return {"ok": step == "code", "step": step, "typed": shown,
                    "code": ("" if step == "code" else
                             "2FA password required" if step == "password" else
                             "this number has no Eitaa account")}
        err = await page_error(page)
        if err:
            print(f"[login-ui] the form reported: {err}", flush=True)
            return {"ok": False, "step": "phone", "typed": shown, "code": err}
        await page.wait_for_timeout(500)

    err = await page_error(page)
    return {"ok": False, "step": await current_step(page), "typed": shown,
            "code": err or f"the page did not reach the code step within {wait_s:.0f}s"}


async def submit_code(driver: Any, code: str, wait_s: float = 60.0) -> dict:
    """Type the received code. tweb submits by itself once it is long enough."""
    page = driver.page
    digits = re.sub(r"\D", "", str(code or ""))
    if not digits:
        return {"ok": False, "code": "no digits in the code"}

    field, sel = await _visible(page, CODE_FIELD, timeout_ms=15000)
    if field is None:
        return {"ok": False, "code": "no code field on screen"}
    shown = await _type_into(page, field, digits)
    print(f"[login-ui] typed the code into {sel} ({len(digits)} digits)", flush=True)

    # tweb auto-submits; if a build does not, the continue button is the backup.
    deadline = time.time() + wait_s
    pressed = False
    while time.time() < deadline:
        step = await current_step(page)
        if step == "password":
            return {"ok": False, "needs_password": True,
                    "code": "SESSION_PASSWORD_NEEDED"}
        if step in ("none",):
            # the auth pages are gone -> the app moved on
            print("[login-ui] the login pages are gone; the app accepted the code",
                  flush=True)
            return {"ok": True, "step": "done", "typed_len": len(digits)}
        err = await page_error(page)
        if err:
            return {"ok": False, "step": step, "code": err}
        if not pressed and time.time() - (deadline - wait_s) > 6:
            btn, _ = await _visible(page, CONTINUE_BUTTON, timeout_ms=1000)
            if btn is not None:
                try:
                    if not await btn.is_disabled():
                        await btn.click(timeout=5000)
                        pressed = True
                        print("[login-ui] pressed continue on the code step",
                              flush=True)
                except Exception:  # noqa: BLE001
                    pass
        await page.wait_for_timeout(500)

    return {"ok": False, "step": await current_step(page),
            "code": f"the code step did not clear within {wait_s:.0f}s"}
