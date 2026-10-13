"""Tests for the form-driven login (eitaa/login_ui.py + login_flow routing).

Run: python -m bot.tests.test_login_ui

Offline: a fake page stands in for Playwright - it records clicks and typing,
and can be told to advance to the next login step when the continue button is
pressed, so the whole phone -> code -> done sequence is exercised without a
browser or a network.

What these tests encode, all of it learned from live runs:

* The phone field is pre-filled with "+98" from the country selector, so the
  country code must NOT be typed again. `989104938987` must be typed as
  `9104938987`; typing the full number produced "+98989…", which the page
  refuses.
* The fields are `div[contenteditable]`, not `input` - typing must append at the
  caret rather than replace a value.
* Exactly ONE submit. Asking for codes repeatedly is what rate-limits a number,
  so the test asserts the click count is 1 even on the paths that fail.
* tweb's step classes (.page-sign / .page-authCode / .page-password /
  .page-signUp) decide what happened; a password step means 2FA and a signup
  step means the number has no account.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="mkwl_loginui_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")

from eitaa import login_ui  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(coro):
    # Closed explicitly: an unclosed loop is garbage-collected at exit and on
    # python 3.9 that prints a "Invalid file descriptor: -1" traceback AFTER
    # the results, which looks like a test failure and is not one.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# a fake Playwright page
# ---------------------------------------------------------------------------

class FakeLocator:
    def __init__(self, page, sel):
        self.page = page
        self.sel = sel
        self.first = self

    def _el(self):
        return self.page.elements.get(self.sel)

    async def is_visible(self):
        el = self._el()
        return bool(el and el.get("visible", True))

    async def is_disabled(self):
        el = self._el()
        return bool(el and el.get("disabled", False))

    async def inner_text(self):
        el = self._el()
        return (el or {}).get("text", "")

    async def input_value(self):
        return (self._el() or {}).get("text", "")

    async def click(self, timeout=None):
        el = self._el()
        if el is None:
            raise RuntimeError(f"no element for {self.sel}")
        if el.get("disabled"):
            raise RuntimeError("element is disabled")
        self.page.clicks.append(self.sel)
        after = el.get("on_click")
        if callable(after):
            after(self.page)

    async def type(self, text, delay=None, timeout=None):
        el = self._el()
        if el is None:
            raise RuntimeError(f"no element for {self.sel}")
        if el.get("reject_typing"):
            return
        el["text"] = el.get("text", "") + text
        self.page.typed.append((self.sel, text))


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.keys.append(key)


class FakePage:
    """elements: selector -> {visible, text, disabled, on_click, reject_typing}

    `present` is the set of selectors that exist in the DOM for the
    querySelector probe current_step() uses.
    """

    def __init__(self, elements=None, present=None, chat_rows=0,
                 app_logged_in=False, my_id=0):
        self.elements = elements or {}
        self.present = set(present or [])
        self.chat_rows = chat_rows
        # what the app answers for the logged-in check (logged_in.js)
        self.app_logged_in = app_logged_in
        self.my_id = my_id
        self.on_reload = None
        self.clicks: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.keys: list[str] = []
        self.reloads = 0
        self.gotos = 0
        self.keyboard = FakeKeyboard(self)

    def locator(self, sel):
        return FakeLocator(self, sel)

    async def evaluate(self, js, arg=None):
        if isinstance(arg, str):            # the querySelector existence probe
            return arg in self.present
        # logged_in.js: an async IIFE returning the decision dict
        if "logged_in" in js and "rootScope" in js:
            return {"logged_in": bool(self.app_logged_in),
                    "how": "rootScope.getMyId" if self.app_logged_in else None,
                    "my_id": self.my_id, "auth_state":
                        "authStateSignedIn" if self.app_logged_in else "authStateSignIn",
                    "chat_rows": self.chat_rows}
        # api_resolver.js injection + its typeof probe
        if "__MKWL_api" in js:
            return True
        if "chatlist-chat" in js:
            return self.chat_rows > 0 if ">" in js else self.chat_rows
        return None

    async def wait_for_timeout(self, ms):
        # MUST actually yield. An immediate return does not hand control back
        # to the event loop, so a polling loop in login_ui would spin for its
        # entire deadline and never see a change the test makes "later" - the
        # harness would then report a timeout for code that works. Scaled down
        # so the suite stays fast.
        await asyncio.sleep(min(float(ms), 500) / 1000.0 / 25.0)

    async def reload(self, wait_until=None):
        self.reloads += 1
        if callable(self.on_reload):
            self.on_reload(self)

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos += 1


class FakeDriver:
    def __init__(self, page):
        self.page = page


PHONE_SEL = ".input-field-phone div.input-field-input[contenteditable='true']"
BTN_SEL = ".page-sign button.btn-primary"
CODE_SEL = ".page-authCode div.input-field-input[contenteditable='true']"


def phone_page(**over):
    """A page sitting on the phone step, as the live one was recorded."""
    def advance_to_code(page):
        page.present.discard(login_ui.PAGE_PHONE)
        page.present.add(login_ui.PAGE_CODE)
        page.elements[login_ui.PAGE_PHONE] = {"visible": False}
        page.elements[login_ui.PAGE_CODE] = {"visible": True}
        page.elements[CODE_SEL] = {"visible": True, "text": ""}

    els = {
        PHONE_SEL: {"visible": True, "text": "+98"},
        BTN_SEL: {"visible": True, "text": "ادامه", "on_click": advance_to_code},
        login_ui.PAGE_PHONE: {"visible": True},
    }
    els.update(over.pop("elements", {}))
    p = FakePage(elements=els,
                 present=over.pop("present", {login_ui.PAGE_PHONE}))
    for k, v in over.items():
        setattr(p, k, v)
    return p


# ---------------------------------------------------------------------------
# the country-code trap
# ---------------------------------------------------------------------------

def test_national_part():
    cases = [
        ("989104938987", "9104938987"),   # the real account used live
        ("989939094655", "9939094655"),
        ("+98 910 493 8987", "9104938987"),
        ("9104938987", "9104938987"),     # already national, leave alone
        ("", ""),
    ]
    for raw, want in cases:
        got = login_ui.national_part(raw)
        check(f"national_part({raw!r}) == {want!r}", got == want, got)
    # The trap itself: the field already shows +98, so the country code must go.
    check("the country code is never typed twice",
          not login_ui.national_part("989104938987").startswith("98"),
          "typing it again produced '+98989…', which the page rejects")


# ---------------------------------------------------------------------------
# step detection
# ---------------------------------------------------------------------------

def test_current_step():
    p = phone_page()
    check("step is 'phone' on the login form", run(login_ui.current_step(p)) == "phone")

    p2 = FakePage(elements={login_ui.PAGE_CODE: {"visible": True}},
                  present={login_ui.PAGE_CODE})
    check("step is 'code' on the code form", run(login_ui.current_step(p2)) == "code")

    p3 = FakePage(elements={login_ui.PAGE_PASSWORD: {"visible": True}},
                  present={login_ui.PAGE_PASSWORD})
    check("step is 'password' for 2FA", run(login_ui.current_step(p3)) == "password")

    p4 = FakePage()
    check("step is 'none' once the login pages are gone",
          run(login_ui.current_step(p4)) == "none")

    # present in the DOM but hidden: tweb keeps every step mounted
    p5 = FakePage(elements={login_ui.PAGE_PHONE: {"visible": False}},
                  present={login_ui.PAGE_PHONE})
    check("a hidden step does not count as the current step",
          run(login_ui.current_step(p5)) == "none",
          "tweb keeps all steps in the DOM and shows one")

    # THE LIVE BREAK (2026-10-13): this build drew the phone step under
    # `.page-signIn`, but current_step keyed only on `.page-sign`, returned
    # "none", and the retry burned a code into FLOOD_WAIT. Both wrapper names
    # must count as the phone step.
    p6 = FakePage(elements={".page-signIn": {"visible": True}},
                  present={".page-signIn"})
    check("step is 'phone' when the wrapper is .page-signIn (the build that broke)",
          run(login_ui.current_step(p6)) == "phone")

    # And if a build renames the wrapper entirely, the visible phone FIELD still
    # identifies the step (login_diag saw the form while current_step did not).
    p7 = FakePage(elements={".input-field-phone [contenteditable='true']":
                            {"visible": True, "text": "+98"}},
                  present=set())
    check("step is 'phone' by its field when the wrapper class is unknown",
          run(login_ui.current_step(p7)) == "phone")

    # The fallback must not mistake the code step for phone: a visible code
    # field with no phone field is still 'code'.
    p8 = FakePage(elements={".page-authCode [contenteditable='true']":
                            {"visible": True}},
                  present=set())
    check("step is 'code' by its field when the wrapper class is unknown",
          run(login_ui.current_step(p8)) == "code")


# ---------------------------------------------------------------------------
# requesting a code
# ---------------------------------------------------------------------------

def test_request_code_happy_path():
    p = phone_page()
    res = run(login_ui.request_code(FakeDriver(p), "989104938987"))
    check("request_code succeeds", res.get("ok") is True, str(res))
    check("it reports the code step", res.get("step") == "code", str(res.get("step")))
    check("only the national part was typed",
          p.typed == [(PHONE_SEL, "9104938987")], str(p.typed))
    check("the caret was moved to the end before typing", "End" in p.keys,
          "a contenteditable keeps the +98 prefix, so typing must append")
    check("the field ended up with the whole number",
          re.sub(r"\D", "", p.elements[PHONE_SEL]["text"]) == "989104938987",
          p.elements[PHONE_SEL]["text"])
    submits = [c for c in p.clicks if c == BTN_SEL]
    check("exactly ONE submit", len(submits) == 1,
          f"button clicks={len(submits)}, all clicks={p.clicks}")


def test_request_code_never_submits_twice():
    """Every failure path must leave the click count at most one."""
    # the page never advances
    p = phone_page(elements={BTN_SEL: {"visible": True, "text": "ادامه"}})
    res = run(login_ui.request_code(FakeDriver(p), "989104938987", wait_s=1.0))
    check("a page that never advances fails", res.get("ok") is False, str(res))
    submits = [c for c in p.clicks if c == BTN_SEL]
    check("still only one submit after a timeout", len(submits) == 1,
          f"button clicks={len(submits)} - a retry here is what rate-limits a number")


def test_request_code_disabled_button():
    p = phone_page(elements={BTN_SEL: {"visible": True, "disabled": True}})
    res = run(login_ui.request_code(FakeDriver(p), "989104938987"))
    check("a disabled continue button is reported, not clicked",
          res.get("ok") is False and "disabled" in str(res.get("code")), str(res))
    check("nothing was submitted", BTN_SEL not in p.clicks, str(p.clicks))


def test_request_code_field_refuses_input():
    p = phone_page(elements={PHONE_SEL: {"visible": True, "text": "+98",
                                         "reject_typing": True}})
    res = run(login_ui.request_code(FakeDriver(p), "989104938987"))
    check("a field that will not accept the number fails fast",
          res.get("ok") is False and "did not accept" in str(res.get("code")),
          str(res.get("code")))
    check("no submit when the number never went in", BTN_SEL not in p.clicks,
          str(p.clicks))


def test_request_code_no_form():
    p = FakePage()
    res = run(login_ui.request_code(FakeDriver(p), "989104938987", form_wait_s=1.0))
    check("a page with no form at all is reported clearly",
          res.get("ok") is False and "no login form appeared" in str(res.get("code")),
          str(res.get("code")))
    check("it tried a reload before giving up", p.reloads >= 1,
          f"{p.reloads} reload(s)")


def test_request_code_waits_for_a_slow_form():
    """The live failure: 'the login form is not on screen' on a page that was
    simply still booting. MKWL_WARMPATH reuses an already-open page instead of
    navigating, and tweb mounts its login steps before revealing one, so asking
    once is not enough."""
    p = FakePage()                     # nothing on screen yet

    async def drive():
        task = asyncio.ensure_future(login_ui.request_code(
            FakeDriver(p), "989104938987", form_wait_s=5.0))
        await asyncio.sleep(0.05)      # the app is still booting
        # now the form appears, exactly as it does after a warm page settles
        def advance(page):
            page.present = {login_ui.PAGE_CODE}
            page.elements[login_ui.PAGE_CODE] = {"visible": True}
            page.elements[CODE_SEL] = {"visible": True, "text": ""}
        p.elements.update({
            PHONE_SEL: {"visible": True, "text": "+98"},
            BTN_SEL: {"visible": True, "on_click": advance},
            login_ui.PAGE_PHONE: {"visible": True},
        })
        p.present = {login_ui.PAGE_PHONE}
        return await task

    res = run(drive())
    check("a form that appears late is still used", res.get("ok") is True, str(res))


def test_request_code_already_logged_in():
    """An account that is already in must say so, not 'no form'."""
    p = FakePage(chat_rows=12, app_logged_in=True)
    res = run(login_ui.request_code(FakeDriver(p), "989104938987", form_wait_s=2.0))
    check("an already-logged-in account is recognised",
          res.get("step") == "logged_in"
          and "already logged in" in str(res.get("code")), str(res))
    check("nothing was typed or submitted for it",
          not p.typed and not p.clicks, f"typed={p.typed} clicks={p.clicks}")


def test_already_logged_in_account_with_no_chats():
    """The live failure. A freshly created account has NO chats, so counting
    .chatlist-chat rows reported zero and an account that had logged in seconds
    earlier came back as "no login form appeared" - which then looked like a
    boot problem and invited another code request that Eitaa rate-limited."""
    p = FakePage(chat_rows=0, app_logged_in=True, my_id=778899)
    check("zero chats does not mean logged out",
          run(login_ui.looks_logged_in(p)) is True,
          "asks the app (getMyId / authState) instead of counting chat rows")
    res = run(login_ui.request_code(FakeDriver(p), "989336724339", form_wait_s=2.0))
    check("an empty but logged-in account is reported as logged in",
          res.get("step") == "logged_in", str(res))
    check("and no code is requested for it", not p.clicks, str(p.clicks))


def test_logged_in_wins_over_a_visible_form():
    """Measured live: for ~3s after the code is accepted the app is already
    authenticated while the login pages are STILL visible -

        t+0 after login : alive=True  authState=authStateSignedIn  form=True
        t+3s            : alive=True  authState=authStateSignedIn  form=False

    Checking the form first inside that window makes a caller type a number and
    start a fresh login, destroying the session that just succeeded. That is
    what happened to a live account: logged in 17:55:13, form filled again
    17:55:43, FLOOD_WAIT_999 one second later."""
    p = phone_page()                      # the phone form IS on screen
    p.app_logged_in = True                # and the app is already logged in
    p.my_id = 68526377
    check("logged-in wins over a visible login form",
          run(login_ui.wait_for_login_form(p, timeout_s=2.0)) == "logged_in",
          "the form must not be preferred during the overlap window")
    res = run(login_ui.request_code(FakeDriver(p), "989104938987", form_wait_s=2.0))
    check("no code is requested in the overlap window",
          res.get("step") == "logged_in" and not p.clicks,
          f"clicks={p.clicks}")
    check("and nothing was typed into the form", not p.typed, str(p.typed))


def test_session_that_appears_while_typing_is_not_overwritten():
    """Typing takes a few seconds. If a session finishes restoring in that
    window, submitting would start a new login and discard it."""
    p = phone_page()

    # the field is real, but the app logs in during the typing
    class _Field(FakeLocator):
        async def type(self, text, delay=None, timeout=None):
            await FakeLocator.type(self, text, delay=delay, timeout=timeout)
            self.page.app_logged_in = True
            self.page.my_id = 68526377

    orig = p.locator

    def locator(sel):
        loc = _Field(p, sel) if sel == PHONE_SEL else orig(sel)
        loc.first = loc
        return loc

    p.locator = locator
    res = run(login_ui.request_code(FakeDriver(p), "989104938987"))
    check("a session that appears while typing stops the submit",
          res.get("step") == "logged_in", str(res))
    check("the continue button was never pressed", BTN_SEL not in p.clicks,
          str(p.clicks))


def test_logged_out_account_is_not_mistaken_for_logged_in():
    """The opposite error would be worse: never offering the form."""
    p = phone_page()
    p.app_logged_in = False
    p.my_id = 0
    check("a logged-out account still gets the form",
          run(login_ui.looks_logged_in(p)) is False)
    res = run(login_ui.request_code(FakeDriver(p), "989104938987"))
    check("and the login proceeds normally", res.get("ok") is True, str(res))


def test_session_restored_by_the_reload_is_noticed():
    """A reload can restore a stored session, and restoring takes a moment, so
    the recovery steps must re-ask instead of pressing on to the form."""
    p = FakePage()                      # nothing on screen, not logged in yet

    def on_reload(page):
        page.app_logged_in = True       # the session comes back
        page.my_id = 4242

    p.on_reload = on_reload
    res = run(login_ui.request_code(FakeDriver(p), "989104938987", form_wait_s=3.0))
    check("a session restored by the reload is reported as logged in",
          res.get("step") == "logged_in", str(res))


def test_request_code_navigates_a_warm_page_that_is_elsewhere():
    p = FakePage()
    run(login_ui.request_code(FakeDriver(p), "989104938987", form_wait_s=1.5))
    check("it navigates to Eitaa when a warm page is somewhere else",
          p.gotos >= 1, f"{p.gotos} navigation(s)")


def test_persian_digits_in_a_flood_message():
    """Live: Eitaa answered FLOOD_WAIT_۷۹۱ in Persian digits, which a `\\d+`
    parser cannot read - so the wait was invisible to us and to the panel."""
    check("Persian digits are normalised",
          login_ui.normalize_digits("FLOOD_WAIT_۷۹۱") == "FLOOD_WAIT_791",
          login_ui.normalize_digits("FLOOD_WAIT_۷۹۱"))
    check("Arabic-Indic digits too",
          login_ui.normalize_digits("FLOOD_WAIT_٧٩١") == "FLOOD_WAIT_791")
    check("the wait is parsed from Persian digits",
          login_ui.flood_seconds("FLOOD_WAIT_۷۹۱") == 791,
          str(login_ui.flood_seconds("FLOOD_WAIT_۷۹۱")))
    check("and from ASCII", login_ui.flood_seconds("FLOOD_WAIT_60") == 60)
    check("a non-flood message yields nothing",
          login_ui.flood_seconds("کد اشتباه است") is None)

    # end to end: the message the panel shows must contain readable digits,
    # because runner.py keys its RATE LIMITED card off the word FLOOD
    def show_flood(page):
        page.elements[".toast"] = {"visible": True, "text": "FLOOD_WAIT_۷۹۱"}
    p = phone_page(elements={BTN_SEL: {"visible": True, "on_click": show_flood}})
    res = run(login_ui.request_code(FakeDriver(p), "989104938987", wait_s=2.0))
    msg = str(res.get("code"))
    check("the panel gets a readable flood message",
          "791" in msg and "FLOOD" in msg.upper(), msg)
    check("it says how long to wait", "13m" in msg or "11s" in msg, msg)


def test_request_code_2fa_and_signup():
    def to_password(page):
        page.present = {login_ui.PAGE_PASSWORD}
        page.elements[login_ui.PAGE_PASSWORD] = {"visible": True}
    p = phone_page(elements={BTN_SEL: {"visible": True, "on_click": to_password}})
    res = run(login_ui.request_code(FakeDriver(p), "989104938987"))
    check("a 2FA page is reported as a password step",
          res.get("ok") is False and res.get("step") == "password", str(res))

    def to_signup(page):
        page.present = {login_ui.PAGE_SIGNUP}
        page.elements[login_ui.PAGE_SIGNUP] = {"visible": True}
    p2 = phone_page(elements={BTN_SEL: {"visible": True, "on_click": to_signup}})
    res2 = run(login_ui.request_code(FakeDriver(p2), "989104938987"))
    check("a number with no account is reported, not retried",
          res2.get("ok") is False and res2.get("step") == "signup", str(res2))


def test_request_code_surfaces_a_form_error():
    """A FLOOD or 'invalid number' message must come back verbatim so the
    caller can stop instead of asking again."""
    def show_error(page):
        page.elements[".toast"] = {"visible": True,
                                   "text": "FLOOD_WAIT_3600 لطفا بعدا تلاش کنید"}
    p = phone_page(elements={BTN_SEL: {"visible": True, "on_click": show_error}})
    res = run(login_ui.request_code(FakeDriver(p), "989104938987", wait_s=2.0))
    check("the form's own error is returned verbatim",
          res.get("ok") is False and "FLOOD_WAIT_3600" in str(res.get("code")),
          str(res.get("code")))


def test_request_code_reloads_a_stale_form():
    """A half-finished attempt from a previous run must be reset, not typed
    into."""
    p = FakePage(elements={login_ui.PAGE_CODE: {"visible": True},
                           PHONE_SEL: {"visible": True, "text": "+98"},
                           BTN_SEL: {"visible": True}},
                 present={login_ui.PAGE_CODE})
    run(login_ui.request_code(FakeDriver(p), "989104938987", wait_s=1.0))
    check("a stale code step triggers a reload first", p.reloads == 1,
          f"{p.reloads} reload(s)")


# ---------------------------------------------------------------------------
# submitting the code
# ---------------------------------------------------------------------------

def test_submit_code_happy_path():
    def clear_pages(page):
        page.present = set()
        page.elements[login_ui.PAGE_CODE] = {"visible": False}
    p = FakePage(elements={CODE_SEL: {"visible": True, "text": ""},
                           login_ui.PAGE_CODE: {"visible": True},
                           BTN_SEL: {"visible": True, "on_click": clear_pages}},
                 present={login_ui.PAGE_CODE})
    # tweb submits by itself: clear the pages as soon as the digits are in
    p.elements[CODE_SEL]["text"] = ""

    async def drive():
        task = asyncio.ensure_future(login_ui.submit_code(FakeDriver(p), "12345",
                                                          wait_s=3.0))
        await asyncio.sleep(0)
        clear_pages(p)                      # the app accepted it
        return await task

    res = run(drive())
    check("submit_code succeeds once the login pages disappear",
          res.get("ok") is True, str(res))
    check("the code digits were typed", p.typed == [(CODE_SEL, "12345")], str(p.typed))


def test_submit_code_wrong_code():
    p = FakePage(elements={CODE_SEL: {"visible": True, "text": ""},
                           login_ui.PAGE_CODE: {"visible": True},
                           ".toast": {"visible": True, "text": "کد اشتباه است"}},
                 present={login_ui.PAGE_CODE})
    res = run(login_ui.submit_code(FakeDriver(p), "11111", wait_s=2.0))
    check("a refused code reports the page's message",
          res.get("ok") is False and "اشتباه" in str(res.get("code")),
          str(res.get("code")))


def test_submit_code_2fa():
    p = FakePage(elements={CODE_SEL: {"visible": True, "text": ""},
                           login_ui.PAGE_PASSWORD: {"visible": True}},
                 present={login_ui.PAGE_PASSWORD, login_ui.PAGE_CODE})
    p.elements[login_ui.PAGE_CODE] = {"visible": False}
    res = run(login_ui.submit_code(FakeDriver(p), "12345", wait_s=2.0))
    check("a password step after the code is flagged as 2FA",
          res.get("needs_password") is True
          and res.get("code") == "SESSION_PASSWORD_NEEDED", str(res))


def test_submit_code_detects_app_login_even_if_dom_lingers():
    """THE live FLOOD cause. After a correct code the app is authenticated but
    the login DOM lingers for a few seconds. submit_code keyed success only on
    the login pages being gone, so a sign-in that ACTUALLY SUCCEEDED reported
    'the code step did not clear'; the owner thought it failed, tapped Add
    Account again, and the repeated code requests tripped FLOOD_WAIT. It must
    ask the app (getMyId / authState) and see the success immediately."""
    p = FakePage(elements={CODE_SEL: {"visible": True, "text": ""},
                           login_ui.PAGE_CODE: {"visible": True}},
                 present={login_ui.PAGE_CODE},
                 app_logged_in=True, my_id=55667788)
    res = run(login_ui.submit_code(FakeDriver(p), "12345", wait_s=2.0))
    check("a successful sign-in is seen via the app even while the code DOM "
          "lingers", res.get("ok") is True, str(res))
    check("the code was still typed", p.typed == [(CODE_SEL, "12345")], str(p.typed))


def test_submit_code_rejects_empty():
    p = FakePage()
    res = run(login_ui.submit_code(FakeDriver(p), "   "))
    check("an empty code is refused before touching the page",
          res.get("ok") is False and "no digits" in str(res.get("code")),
          str(res.get("code")))


# ---------------------------------------------------------------------------
# routing: bot/runner.py must keep working unchanged
# ---------------------------------------------------------------------------

def test_login_flow_routes_to_the_form_by_default():
    from eitaa import login_flow
    check("the form is the default login path", login_flow.LOGIN_MODE == "ui",
          f"MKWL_LOGIN_MODE={login_flow.LOGIN_MODE}")
    check("the API path is still reachable for an older build",
          "MKWL_LOGIN_MODE" in open(login_flow.__file__).read())

    # runner.py refuses to ask the owner for a code unless phone_code_hash is
    # non-empty, so the form path has to report one.
    p = phone_page()
    res = run(login_flow.send_code(FakeDriver(p), "989104938987", 0, ""))
    check("send_code returns a non-empty phone_code_hash on the form path",
          res.get("ok") and res.get("phone_code_hash") == login_flow.UI_HASH,
          str(res))

    # and sign_in must recognise that hash and go back to the form
    def clear_pages(page):
        page.present = set()
    p2 = FakePage(elements={CODE_SEL: {"visible": True, "text": ""},
                            login_ui.PAGE_CODE: {"visible": True}},
                  present={login_ui.PAGE_CODE})

    async def drive():
        task = asyncio.ensure_future(login_flow.sign_in(
            FakeDriver(p2), "989104938987", login_flow.UI_HASH, "12345"))
        await asyncio.sleep(0)
        clear_pages(p2)
        return await task

    res2 = run(drive())
    check("sign_in follows the same path send_code used",
          res2.get("ok") is True and res2.get("mode") == "ui", str(res2))


def test_api_path_is_documented_as_broken():
    """The API path must not look like a neutral alternative: on this build it
    cannot persist a session."""
    src = open(__import__("eitaa.login_flow", fromlist=["x"]).__file__).read()
    check("the API path's limitation is written down where it is chosen",
          "INVALID_LOGIN" in src and "authStateSignIn" in src,
          "so nobody switches back to it expecting it to work")


def main() -> int:
    print("=" * 68)
    print("FORM-DRIVEN LOGIN TESTS")
    print("=" * 68)
    try:
        print("\n-- the country-code trap --")
        test_national_part()
        print("\n-- step detection --")
        test_current_step()
        print("\n-- requesting a code --")
        test_request_code_happy_path()
        test_request_code_never_submits_twice()
        test_request_code_disabled_button()
        test_request_code_field_refuses_input()
        test_request_code_no_form()
        test_request_code_waits_for_a_slow_form()
        test_request_code_already_logged_in()
        test_already_logged_in_account_with_no_chats()
        test_logged_out_account_is_not_mistaken_for_logged_in()
        test_logged_in_wins_over_a_visible_form()
        test_session_that_appears_while_typing_is_not_overwritten()
        test_session_restored_by_the_reload_is_noticed()
        test_request_code_navigates_a_warm_page_that_is_elsewhere()
        test_request_code_2fa_and_signup()
        test_request_code_surfaces_a_form_error()
        test_request_code_reloads_a_stale_form()
        test_persian_digits_in_a_flood_message()
        print("\n-- submitting the code --")
        test_submit_code_happy_path()
        test_submit_code_wrong_code()
        test_submit_code_2fa()
        test_submit_code_detects_app_login_even_if_dom_lingers()
        test_submit_code_rejects_empty()
        print("\n-- routing --")
        test_login_flow_routes_to_the_form_by_default()
        test_api_path_is_documented_as_broken()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL FORM-LOGIN TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
