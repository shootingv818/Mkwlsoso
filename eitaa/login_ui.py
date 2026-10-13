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
from pathlib import Path
from typing import Any

# The same app-level logged-in check the driver uses, so "already logged in"
# means the same thing in both places.
try:
    _LOGGED_IN_SRC = (Path(__file__).with_name("logged_in.js")).read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _LOGGED_IN_SRC = ""

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

# tweb has shipped the phone step under BOTH `.page-sign` and `.page-signIn`
# across builds (login_diag keys on the same pair). Keying current_step on only
# `.page-sign` made the bot report "no login form" on a build using `.page-signIn`
# even though the form was on screen - it then retried and burned codes into a
# FLOOD_WAIT. So each step accepts every wrapper class tweb is known to use.
_STEP_WRAPPERS = (
    ("code", [PAGE_CODE, ".page-code"]),
    ("password", [PAGE_PASSWORD]),
    ("signup", [PAGE_SIGNUP, ".page-signup"]),
    ("phone", [PAGE_PHONE, ".page-signIn"]),
)

# Last-resort step detection by the step's OWN field, for when a build renames
# the wrapper entirely or reports it not-visible while the field is on screen.
# Each selector is step-SPECIFIC (never a generic catch-all), so a visible phone
# field cannot be mistaken for the code step and vice-versa. Priority order
# matters: code before phone.
_STEP_FIELD_FALLBACK = (
    ("code", [".page-authCode [contenteditable='true']",
              ".page-authCode input.input-field-input",
              ".page-authCode input"]),
    ("password", ["input[type='password']"]),
    ("phone", [".input-field-phone [contenteditable='true']",
               ".input-field-phone input",
               "input[type='tel']"]),
)

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


async def _first_visible(page: Any, selectors: list[str]):
    """One pass: first visible locator among candidates, or (None, None)."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                return loc, sel
        except Exception:  # noqa: BLE001
            continue
    return None, None


async def _login_dom_snapshot(page: Any):
    """Read-only snapshot for diagnosing a 'no form' timeout: which .page-*
    wrappers exist (with display/visibility/size) and whether a phone/code field
    is present. So the next time a build renames something, the log says exactly
    what to add instead of us guessing. Never raises."""
    try:
        return await page.evaluate(
            "() => { const o={pages:[],fields:{}};"
            "document.querySelectorAll('[class*=\"page-\"]').forEach(e=>{"
            "const r=e.getBoundingClientRect(),c=getComputedStyle(e);"
            "o.pages.push(e.className+' ['+c.display+'/'+c.visibility+' '"
            "+Math.round(r.width)+'x'+Math.round(r.height)+']');});"
            "const q=s=>{try{return !!document.querySelector(s);}catch(e){return false;}};"
            "o.fields={phone:q('.input-field-phone [contenteditable]')||q(\"input[type='tel']\"),"
            "code:q('.page-authCode [contenteditable]'),authPages:q('#auth-pages'),"
            "anyInput:q('input.input-field-input')||q('[contenteditable=\"true\"]')};"
            "return o; }")
    except Exception:  # noqa: BLE001
        return None


async def current_step(page: Any) -> str:
    """Which login step is on screen: phone / code / password / signup / none."""
    # 1) The wrapper class, trying every name tweb has used for the step. tweb
    #    keeps every step mounted and shows one, so visibility decides.
    for name, candidates in _STEP_WRAPPERS:
        for sel in candidates:
            if not await _has(page, sel):
                continue
            try:
                if await page.locator(sel).first.is_visible():
                    return name
            except Exception:  # noqa: BLE001
                continue
    # 2) Fallback: the wrapper may be renamed on a newer build, or reported
    #    not-visible while the field itself is clearly on screen (login_diag saw
    #    the form; current_step returned "none" and the retry burned a code).
    #    Decide by whichever step-specific field is visible, code before phone.
    for name, fields in _STEP_FIELD_FALLBACK:
        loc, _sel = await _first_visible(page, fields)
        if loc is not None:
            return name
    return "none"


# Eitaa renders numbers with Persian/Arabic-Indic digits, so a live FLOOD came
# back as "FLOOD_WAIT_۷۹۱" - unreadable to a `\d+` parser, and to the panel's
# own FLOOD handling. Normalised on the way out so the wait is legible.
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(text: str) -> str:
    return (text or "").translate(_DIGIT_MAP)


def flood_seconds(text: str) -> int | None:
    """Seconds from a FLOOD message, whichever digits it used."""
    m = re.search(r"FLOOD_WAIT_?(\d+)", normalize_digits(text), re.I)
    return int(m.group(1)) if m else None


async def page_error(page: Any) -> str | None:
    """Any error text the form is showing (wrong code, flood, invalid phone)."""
    for sel in _ERROR_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                txt = (await loc.inner_text()).strip()
                if txt:
                    return normalize_digits(txt)[:200]
        except Exception:  # noqa: BLE001
            continue
    return None


async def looks_logged_in(page: Any) -> bool:
    """Is this account already logged in? Used to tell 'no form' from 'already in'.

    This started as `.chatlist-chat` rows > 0 and that was wrong in a way a live
    run exposed immediately: a freshly created account has NO chats, so zero
    rows, so an account that had logged in seconds earlier was reported as "no
    login form appeared". Asking the app is both correct and cheaper - the same
    check eitaa/logged_in.js does for the driver.
    """
    if _LOGGED_IN_SRC:
        try:
            from eitaa.api_resolver import ensure_api_resolver
            await ensure_api_resolver(page)
            res = await page.evaluate(_LOGGED_IN_SRC)
            if isinstance(res, dict):
                if res.get("logged_in"):
                    print(f"[login-ui] already logged in (via {res.get('how')}, "
                          f"id={res.get('my_id')})", flush=True)
                    return True
                return False
        except Exception:  # noqa: BLE001
            pass
    # DOM fallback only: an account WITH chats still answers correctly here.
    try:
        return bool(await page.evaluate(
            "() => document.querySelectorAll('.chatlist-chat').length > 0"))
    except Exception:  # noqa: BLE001
        return False


async def wait_for_login_form(page: Any, timeout_s: float = 45.0) -> str:
    """Wait until a login step is actually VISIBLE, and recover if it is not.

    The first version of this asked once and gave up, which produced "the login
    form is not on screen" on a page that was simply still booting. Two things
    make that likely on this host: MKWL_WARMPATH reuses an already-open page
    rather than navigating, so the page may be showing something else entirely;
    and tweb mounts all of its login steps and reveals one, so there is a window
    where `.page-sign` exists but is not visible yet.

    So: poll, then try one reload, then one explicit navigation, before
    concluding the form is not there. Returns the step name, "logged_in", or
    "none".
    """
    from config import config

    deadline = time.time() + timeout_s
    reloaded = False
    navigated = False
    while time.time() < deadline:
        # LOGGED-IN IS CHECKED FIRST, and that order is not cosmetic. Measured
        # on a live login: for about three seconds after the code is accepted
        # the app is already authenticated while the login pages are STILL on
        # screen -
        #
        #     t+0 after login : alive=True  authState=authStateSignedIn  form=True
        #     t+3s            : alive=True  authState=authStateSignedIn  form=False
        #
        # Asking current_step() first would return "phone" inside that window, a
        # caller would type a number into the form, and that starts a fresh
        # login which DESTROYS the session that just succeeded. That is exactly
        # what happened to an account live: logged in at 17:55:13, the form
        # filled again at 17:55:43, and FLOOD_WAIT_999 one second later.
        if await looks_logged_in(page):
            return "logged_in"
        step = await current_step(page)
        if step != "none":
            return step
        left = deadline - time.time()
        # A stuck SPA boot is fixed by a reload; a slow one is only made worse,
        # so this happens once, at the two-thirds mark.
        if not reloaded and left < timeout_s / 3 * 2:
            reloaded = True
            print("[login-ui] no login form yet - reloading once", flush=True)
            try:
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
            except Exception:  # noqa: BLE001
                pass
            # A reload can restore a stored session, which takes a moment to
            # settle - so ask again before treating the missing form as a fault.
            if await looks_logged_in(page):
                return "logged_in"
        # And if the page was never on Eitaa to begin with (a warm page left
        # elsewhere), navigate explicitly.
        elif not navigated and left < timeout_s / 3:
            navigated = True
            print(f"[login-ui] still no form - navigating to "
                  f"{config.EITAA_WEB_URL}", flush=True)
            try:
                await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(4000)
            except Exception:  # noqa: BLE001
                pass
            if await looks_logged_in(page):
                return "logged_in"
        await page.wait_for_timeout(500)
    snap = await _login_dom_snapshot(page)
    if snap:
        print(f"[login-ui] giving up after {timeout_s:.0f}s: no step detected. "
              f"DOM page-* = {snap.get('pages')}  fields = {snap.get('fields')}",
              flush=True)
    return "none"


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
                       wait_s: float = 45.0,
                       form_wait_s: float = 45.0) -> dict:
    """Fill the phone in and press continue, ONCE.

    Returns {ok, step, typed, detail}. `step` is where the page ended up:
    "code" is success, "password" means 2FA before any code, "signup" means the
    number has no Eitaa account, "phone" means the form refused it.
    """
    page = driver.page
    step = await wait_for_login_form(page, timeout_s=form_wait_s)
    if step == "logged_in":
        return {"ok": False, "step": "logged_in",
                "code": "this account is already logged in"}
    if step == "none":
        return {"ok": False, "step": "none",
                "code": f"no login form appeared within {form_wait_s:.0f}s, after a "
                        f"reload and a fresh navigation - the app did not boot"}
    if step != "phone":
        # A half-finished attempt from a previous run: reload to get a clean form.
        print(f"[login-ui] page was on the '{step}' step; reloading to start clean",
              flush=True)
        try:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
        except Exception:  # noqa: BLE001
            pass
        step = await wait_for_login_form(page, timeout_s=form_wait_s)
        if step != "phone":
            return {"ok": False, "step": step,
                    "code": f"after reloading, the form was on the '{step}' step "
                            f"instead of the phone step"}

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

    # Last gate before the irreversible step. Typing took a few seconds, and a
    # session can finish restoring in that time; submitting the form then starts
    # a new login and throws away the session that already exists. Cheap check,
    # and it is the only thing standing between a race and a destroyed account.
    if await looks_logged_in(page):
        return {"ok": False, "step": "logged_in",
                "code": "this account logged in while the form was being filled; "
                        "not submitting, because that would discard the session"}
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
            fw = flood_seconds(err)
            if fw is not None:
                err = (f"{err}  (rate limited for {fw}s ~ {fw // 60}m "
                       f"{fw % 60}s - wait it out, do not retry)")
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
        # Ask the APP first, not just the DOM. The decisive "did it work" signal
        # is the account being authenticated (rootScope.getMyId / authState).
        # The login DOM can LINGER for a few seconds after a successful sign-in,
        # so keying success only on "the login pages are gone" made a sign-in
        # that ACTUALLY SUCCEEDED report "the code step did not clear" - which
        # looked like a failure and invited another code request, and THAT is
        # what tripped FLOOD_WAIT even though the user had just logged in.
        if await looks_logged_in(page):
            print("[login-ui] app reports authenticated; the code was accepted",
                  flush=True)
            return {"ok": True, "step": "done", "typed_len": len(digits)}
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

    # One last authoritative check: a sign-in that completed right at the
    # deadline must not be reported as a failure (a false failure here is what
    # drives a retry into FLOOD_WAIT).
    if await looks_logged_in(page):
        return {"ok": True, "step": "done", "typed_len": len(digits)}
    return {"ok": False, "step": await current_step(page),
            "code": f"the code step did not clear within {wait_s:.0f}s"}
