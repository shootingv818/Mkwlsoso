#!/usr/bin/env python3
"""Why does a SUCCESSFUL login read as "not confirmed"? And can contacts load?

State as of the first live run with the resolver fix
----------------------------------------------------
The login itself now works:

    OK  request login code  (0s)      <- used to fail here with
                                         "auth bridge unavailable"
    OK  sign in with your code (11s)  <- auth.signIn accepted
    .. and Eitaa sent its own "login to your account" notification

Then `_wait_logged_in()` polled for 120 s and gave up, and Update Contacts
returned nothing.

The prime suspect is NOT the session. `EitaaDriver.is_logged_in()` looks for
`eitaa/selectors.py: SEARCH_INPUT`, and every candidate in that list is an
`input` element:

    input.input-search-input, input.input-field-input.input-search-input,
    #column-left input.input-field-input, .input-search input, ...

On this build Eitaa's fields are `div` elements with `contenteditable="true"` -
the login form itself proved that: its country and phone fields are
`div.input-field-input`, and the repo's own SEARCH_INPUT list matched NOTHING
while the generic `[contenteditable="true"]` matched both fields. If the search
box became a contenteditable div too, then a fully logged-in app can never be
recognised, no matter how long it is given.

This script settles it on the profile you just logged in with, by asking two
independent questions:

  A. Is the session REAL? Not from the DOM but from the app itself -
     `rootScope.getMyId()` and `appUsersManager.getSelf()` through the resolver.
     A real user id means the login succeeded and only the DETECTION is broken.
  B. What does the chat UI look like NOW? It dumps every input-ish and
     chat-list-ish element with its real tag and classes, and tests both the
     repo's current selectors and a set of contenteditable-aware candidates, so
     the replacement can be chosen from evidence.

It also checks whether the contacts path can read anything (`getContacts` is a
read-only call), since that was the other symptom.

Safety
------
* Read-only. No message is sent, no code is requested, no setting, contact or
  ledger file is written. The only calls are getMyId / getSelf / getState /
  contacts.getContacts, all reads.
* No project file is modified. Output goes to the gitignored artifacts/ dir.

Run on the SERVER, with the account you just logged in:

    cd ~/Mkwlsoso && .venv/bin/python deploy/post_login_probe.py --account 989104938987

Options:
    --wait 25        seconds to let the app boot before looking (default 25)
    --no-contacts    skip the contacts read
    --headed / --no-light / --keep-open N
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"checks": {}, "verdict": []}
CHECKS: list = []
_T0 = time.time()


def banner(t: str) -> None:
    print("\n" + "=" * 74 + f"\n  {t}\n" + "=" * 74, flush=True)


def log(m: str = "") -> None:
    print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)


def ok(m: str) -> None:
    log(f"OK    {m}")


def bad(m: str) -> None:
    log(f"FAIL  {m}")


def info(m: str) -> None:
    log(f"      {m}")


def check(name: str, passed, detail: str = "") -> None:
    CHECKS.append((name, passed, detail))
    REPORT["checks"][name] = {"passed": passed, "detail": detail}
    mark = "OK  " if passed is True else ("FAIL" if passed is False else "WARN")
    log(f"{mark}  [{name}] {detail}")


def dump(label: str, obj, indent: str = "        ") -> None:
    info(label)
    for line in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines():
        print(indent + line, flush=True)


async def ev(page, js: str, arg=None, timeout: float = 25.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout: no answer within {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# Ask the APP whether it is logged in, which no DOM change can break.
IDENTITY_JS = r"""
async () => {
  const out = {};
  const cap = (p, ms) => Promise.race([
    Promise.resolve(p),
    new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms || 8000))
  ]);
  try { out.getMyId = await cap(window.rootScope && window.rootScope.getMyId
        ? window.rootScope.getMyId() : null); }
  catch (e) { out.getMyId = "ERR " + e; }
  try {
    const m = window.__MKWL_managers ? window.__MKWL_managers() : null;
    out.managers = !!m;
    out.source = window.__MKWL_apiSource;
    if (m && m.appUsersManager) {
      try {
        const self = await cap(m.appUsersManager.getSelf());
        out.getSelf = self ? { id: self.id, first_name: self.first_name,
                               phone: self.phone ? "…" + String(self.phone).slice(-4) : null,
                               _: self._ } : self;
      } catch (e) { out.getSelf = "ERR " + e; }
    }
    if (m && m.appStateManager) {
      try {
        const st = await cap(m.appStateManager.getState());
        out.state_keys = st ? Object.keys(st).slice(0, 25) : null;
        out.authState = st && st.authState ? st.authState._ || st.authState : null;
      } catch (e) { out.state_keys = "ERR " + e; }
    }
  } catch (e) { out.managers_error = String(e); }
  try { out.lsKeys = Object.keys(localStorage); } catch (e) { out.lsKeys = ["<blocked>"]; }
  return out;
}
"""

# What the chat UI is actually made of on this build.
DOM_JS = r"""
(repo) => {
  const vis = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
    } catch (e) { return false; }
  };
  const d = (el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute("type") || null,
    cls: String(el.className || "").slice(0, 100) || null,
    ce: el.getAttribute("contenteditable") || null,
    ph: el.getAttribute("placeholder") || el.getAttribute("data-placeholder") || null,
    text: (el.innerText || "").trim().slice(0, 40) || null,
    visible: vis(el)
  });
  const out = {};
  out.url = location.href;
  out.body_head = ((document.body && document.body.innerText) || "").slice(0, 300);
  out.all_inputish = Array.prototype.slice.call(
    document.querySelectorAll("input, [contenteditable='true']")).map(d).slice(0, 30);
  // anything that smells like a search box, by class name, on any tag
  out.search_like = Array.prototype.slice.call(
    document.querySelectorAll("[class*='search'], [class*='Search']")).map(d).slice(0, 25);
  out.chat_rows = document.querySelectorAll(".chatlist-chat").length;
  out.columns = ["#column-left", "#column-center", ".chatlist", ".chatlist-container",
                 ".sidebar-header", "#auth-pages", ".page-sign"].filter(s => {
    try { return !!document.querySelector(s); } catch (e) { return false; }
  });
  // which selectors match: the repo's current list, plus candidates that are
  // aware that fields became contenteditable divs on this build
  const test = (list) => (list || []).map(sel => {
    let n = 0, v = 0;
    try {
      const els = document.querySelectorAll(sel);
      n = els.length;
      els.forEach(e => { if (vis(e)) v++; });
    } catch (e) { return { sel: sel, error: true }; }
    return { sel: sel, matches: n, visible: v };
  });
  out.repo_SEARCH_INPUT = test(repo.SEARCH_INPUT);
  out.candidates = test([
    ".chatlist-chat",
    "#column-left .chatlist-chat",
    "div.input-search-input",
    "[contenteditable='true'].input-search-input",
    ".input-search [contenteditable='true']",
    "#column-left [contenteditable='true']",
    ".sidebar-header .input-search",
    ".input-search",
    "#column-left .input-field-input",
    "#chatlist-container",
    ".chatlist",
    ".sidebar-header",
    "#column-center",
    ".chat-input",
    "div.input-message-input"
  ]);
  return out;
}
"""


async def run(args) -> int:
    REPORT["args"] = vars(args)
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"post_login_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.driver import EitaaDriver
    from eitaa import selectors as S
    from eitaa.api_resolver import ensure_api_resolver, api_info

    url = args.url or config.EITAA_WEB_URL
    light = False if args.no_light else None

    banner("SECTION 0  -  open the profile you just logged in with")
    info("no instrumentation: the recorder used earlier was proven to break the")
    info("very calls it was measuring, so nothing is patched here.")
    t = time.time()
    try:
        session = await BrowserSession(args.account, headed=args.headed,
                                       light_assets=light).start()
    except Exception as exc:  # noqa: BLE001
        bad(f"browser failed to start - {exc}")
        check("browser starts", False, str(exc))
        return 2
    ok(f"browser up in {time.time() - t:.1f}s")
    page = session.page
    driver = EitaaDriver(session)

    try:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            check("page loads", (resp.status if resp else 0) == 200,
                  f"HTTP {resp.status if resp else '?'}")
        except Exception as exc:  # noqa: BLE001
            check("page loads", False, str(exc))
            return 2
        info(f"letting the app boot for {args.wait:.0f}s")
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)
        info(f"resolver: {await api_info(page)}")

        banner("SECTION 1  -  is the SESSION real (ask the app, not the DOM)")
        ident, err = await ev(page, IDENTITY_JS, timeout=30)
        REPORT["identity"] = ident or {"error": err}
        if err or not ident:
            bad(f"could not ask the app - {err}")
            check("session is real", None, str(err))
        else:
            dump("what the app says about itself:", ident)
            my_id = ident.get("getMyId")
            self_ = ident.get("getSelf")
            real = (isinstance(my_id, int) and my_id != 0) or (
                isinstance(self_, dict) and self_.get("id"))
            check("session is real (the app knows who we are)", bool(real),
                  f"getMyId={my_id} getSelf={self_ if not isinstance(self_, dict) else self_.get('id')}")
            ls = ident.get("lsKeys") or []
            check("session data is stored in the profile",
                  any(str(k).startswith("account") for k in ls),
                  f"{len(ls)} localStorage keys: {list(ls)[:10]}")

        banner("SECTION 2  -  what the repo's login DETECTOR sees")
        t0 = time.time()
        detected = await driver.is_logged_in()
        el = time.time() - t0
        check("driver.is_logged_in() returns True", detected,
              f"took {el:.1f}s - this is the function _wait_logged_in() polls for "
              f"120s, and the card you got says it never returned True")

        dom, err2 = await ev(page, DOM_JS,
                             {"SEARCH_INPUT": list(getattr(S, "SEARCH_INPUT", []))},
                             timeout=25)
        REPORT["dom"] = dom or {"error": err2}
        if dom:
            info(f"url: {dom['url']}")
            info("page text:")
            for ln in str(dom["body_head"]).splitlines()[:6]:
                if ln.strip():
                    print("        " + ln.strip()[:100], flush=True)
            info(f"containers present: {dom.get('columns')}")
            info(f"rendered chat rows (.chatlist-chat): {dom.get('chat_rows')}")
            dump("every input / contenteditable on the page:", dom.get("all_inputish"))
            dump("anything with 'search' in its class:", dom.get("search_like"))
            dump("the repo's current SEARCH_INPUT candidates:",
                 dom.get("repo_SEARCH_INPUT"))
            dump("replacement candidates (contenteditable-aware):",
                 dom.get("candidates"))

            repo_hits = [r for r in (dom.get("repo_SEARCH_INPUT") or [])
                         if r.get("visible")]
            cand_hits = [r for r in (dom.get("candidates") or []) if r.get("visible")]
            check("the repo's SEARCH_INPUT list still matches something",
                  bool(repo_hits),
                  f"{len(repo_hits)} visible match(es)"
                  if repo_hits else
                  "NOTHING matches - every candidate is an `input` element, and "
                  "this build uses contenteditable divs")
            check("a working replacement selector exists", bool(cand_hits),
                  json.dumps([c["sel"] for c in cand_hits][:6], ensure_ascii=False)
                  if cand_hits else "none of the candidates matched either")
            check("the chat list is actually rendered",
                  bool(dom.get("chat_rows")) or ".chatlist" in (dom.get("columns") or []),
                  f"{dom.get('chat_rows')} chat rows, containers {dom.get('columns')}")

        if not args.no_contacts:
            banner("SECTION 3  -  can the contacts path read anything (read-only)")
            got = await driver.ensure_contacts_list_bridge()
            check("contacts bridge injects", bool(got))
            if got:
                r, e3 = await ev(page, "() => window.__MKWL_contactsList()", timeout=60)
                REPORT["contacts"] = r if isinstance(r, dict) else {"error": e3, "raw": r}
                if e3:
                    bad(f"contacts call failed - {e3}")
                    check("contacts can be read", False, str(e3))
                elif isinstance(r, dict) and r.get("ok"):
                    items = r.get("contacts") or []
                    ok(f"contacts read: {len(items)} entries "
                       f"(raw={r.get('raw')} skipped={r.get('skipped')})")
                    check("contacts can be read", True,
                          f"{len(items)} contacts - so Update Contacts should work")
                    if items:
                        dump("first two entries (names truncated):",
                             [{k: (str(v)[:12] if k in ("title", "name") else v)
                               for k, v in it.items()} for it in items[:2]])
                else:
                    bad(f"contacts call returned: {json.dumps(r, ensure_ascii=False)[:300]}")
                    check("contacts can be read", False,
                          str(r.get("code") if isinstance(r, dict) else r)[:160])

        try:
            await page.screenshot(path=str(outdir / "page.png"), full_page=False)
            (outdir / "page.html").write_text(await page.content(), encoding="utf-8")
            ok(f"screenshot + DOM saved to {outdir}")
        except Exception:  # noqa: BLE001
            pass
        if args.keep_open:
            await asyncio.sleep(float(args.keep_open))

        banner("RESULT")
        width = max(len(n) for n, _, _ in CHECKS) if CHECKS else 10
        for name, passed, detail in CHECKS:
            mark = "PASS" if passed is True else ("FAIL" if passed is False else "WARN")
            print(f"  {mark}  {name.ljust(width)}  {detail}", flush=True)
        print("", flush=True)

        real = REPORT["checks"].get("session is real (the app knows who we are)", {}).get("passed")
        detect = REPORT["checks"].get("driver.is_logged_in() returns True", {}).get("passed")
        repl = REPORT["checks"].get("a working replacement selector exists", {}).get("passed")
        contacts = REPORT["checks"].get("contacts can be read", {}).get("passed")

        if real and not detect:
            REPORT["verdict"].append(
                "The login SUCCEEDED and only the DETECTOR is wrong: the app knows "
                "who we are, while is_logged_in() - which searches for `input` "
                "elements that this build no longer has - cannot see it. Fix the "
                "detection (ask the app via getMyId/getSelf, with "
                "contenteditable-aware selectors as backup) and 'LOGIN NOT "
                "CONFIRMED' disappears. Nothing is wrong with the session.")
        elif real and detect:
            REPORT["verdict"].append(
                "Both agree the account is logged in now. The 120s card was a "
                "boot-time race: the app simply needed longer than the detector "
                "allowed. Raising MKWL_LOGIN_SETTLE_TIMEOUT is then the whole fix.")
        elif not real:
            REPORT["verdict"].append(
                "The app does NOT report a user, so the session itself did not "
                "survive. The sign-in was accepted but finalize did not stick - "
                "look at the dispatchUserAuth step in login_bridge.js and the "
                "'finalize' list in the signIn result.")
        if contacts is False:
            REPORT["verdict"].append(
                "Contacts could not be read either - see SECTION 3's error. If the "
                "session is real, this is the next thing to fix, separately.")
        elif contacts is True:
            REPORT["verdict"].append(
                "Contacts CAN be read, so once detection is fixed the normal flow "
                "(cache contacts, then send) should work.")
        for i, line in enumerate(REPORT["verdict"], 1):
            print(f"\n  {i}) {line}", flush=True)
    except Exception:  # noqa: BLE001
        REPORT["traceback"] = traceback.format_exc()
        print(traceback.format_exc(), flush=True)
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass

    (outdir / "report.json").write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n  full report: {outdir / 'report.json'}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Diagnose a login that succeeded but was not confirmed. "
                    "Read-only: sends nothing, requests no code.")
    p.add_argument("--account", required=True)
    p.add_argument("--wait", type=float, default=25)
    p.add_argument("--no-contacts", action="store_true")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-light", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--keep-open", type=float, default=0)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
