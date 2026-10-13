#!/usr/bin/env python3
"""Two controls the previous probes left open, and a look at the front door.

Where we are
------------
The preflight said NO-GO on exactly one check: no call through
`rootScope.managers.*` ever answers. The decisive detail was that
`appStateManager.getState()` also timed out - and that one needs no network at
all, it just reads local state. So the earlier theory ("we asked for a method
Eitaa does not implement") is dead.

What the evidence actually shows:

    task type "invokeApi"  ->  immediate error {error:{message:"no listener"}}
    task type "manager"    ->  complete silence, no result frame ever

If nothing listened for `manager` we would have got `no listener` for it too.
We did not. So a listener exists, accepts the task, and never finishes it.

Two explanations remain, and they are tested here as CONTROLS rather than
argued about:

  (1) OBSERVER EFFECT - our own recorder breaks it. To record frames it patches
      MessagePort.prototype.postMessage / onmessage and wraps Worker and
      SharedWorker, and for SharedWorker it calls port.start(). Calling start()
      before the app installs its own onmessage can deliver early messages to
      listeners only, so the app never sees them. The instrument may be
      changing the measurement.
      -> SECTION 1 repeats the calls with NO recorder at all.

  (2) NOT LOGGED IN - manager tasks only resolve once an account exists. No
      account is logged in here (chat UI absent), so accountNumber 1 has no
      loaded state and the handler waits forever. That would be a deadlock: we
      need the path to log in, and the path needs a login.
      -> SECTION 1 distinguishes this too: if the calls still hang with no
      recorder, the recorder is innocent and (2) stands.

And the thing we overlooked
---------------------------
`"login UI drawn": true`, with the page showing a country selector, a phone
field and a "continue" button. The bridges try to reach Eitaa through its
internal API; the actual login FORM is sitting right there, and driving a form
does not care how Eitaa refactors its internals. This repo already has the
pieces for that approach (`eitaa/selectors.py`, `eitaa/driver.py`).

SECTION 2 therefore maps the login form: which selectors match, whether the
fields are editable, what the button says. SECTION 3 can optionally TYPE a
phone number to prove the form is drivable - and it stops before the button.

Safety
------
* No login code is requested. SECTION 3 types into the phone field and then
  STOPS; the continue button is never clicked unless you pass
  --i-want-to-click-continue, which is off by default and prints a warning.
* No API method is called except the three read-only ones from the preflight.
* No existing project file is modified. Output goes to the gitignored
  artifacts/ dir.

Run on the SERVER:

    cd ~/Mkwlsoso && .venv/bin/python deploy/login_ui_probe.py --account 989939094655

    # also type the phone into the form (still never clicks continue):
    .venv/bin/python deploy/login_ui_probe.py --account 989939094655 --fill 989939094655
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

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


async def ev(page, js: str, arg=None, timeout: float = 20.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout: no answer within {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# The same resolver the preflight used, so SECTION 1 is a true control: only
# the recorder differs between the two runs.
RESOLVER_JS = r"""
(() => {
  window.__MKWL_apiSource = null;
  window.__MKWL_managers = function () {
    try {
      const r = window.rootScope && window.rootScope.managers;
      if (r && r.apiManager) { window.__MKWL_apiSource = "rootScope.managers"; return r; }
    } catch (e) {}
    try {
      const f = window.createProxiedManagersForAccount;
      if (typeof f === "function") {
        const m = f(window.__MKWL_accountNumber || 1);
        if (m && m.apiManager) { window.__MKWL_apiSource = "createProxiedManagersForAccount"; return m; }
      }
    } catch (e) {}
    return null;
  };
  window.__MKWL_api = function () {
    try {
      if (window.apiManager && window.apiManager.invokeApi) {
        window.__MKWL_apiSource = "window.apiManager";
        return window.apiManager;
      }
    } catch (e) {}
    const m = window.__MKWL_managers();
    return m ? m.apiManager : null;
  };
})();
"""

CALL_JS = r"""
async (a) => {
  const LIMIT = a.timeout_ms || 8000;
  const t0 = Date.now();
  const out = { label: a.label };
  const cap = (p) => Promise.race([
    p, new Promise((_, rej) => setTimeout(() => rej(new Error("MKWL_TIMEOUT " + LIMIT + "ms")), LIMIT))
  ]);
  let target = null;
  try {
    if (a.on === "api") target = window.__MKWL_api();
    else { const m = window.__MKWL_managers(); target = m ? m[a.on] : null; }
  } catch (e) { out.resolve_error = String(e); }
  out.source = window.__MKWL_apiSource;
  if (!target) { out.ok = false; out.code = "could not resolve " + a.on; return out; }
  try {
    const fn = target[a.fn];
    if (typeof fn !== "function") { out.ok = false; out.code = "no " + a.fn; return out; }
    const r = await cap(Promise.resolve(fn.apply(target, a.args || [])));
    out.ok = true;
    out.ms = Date.now() - t0;
    try { out.value = JSON.stringify(r === undefined ? null : r).slice(0, 500); }
    catch (e) { out.value = "<unserializable>"; }
  } catch (e) {
    out.ok = false;
    out.ms = Date.now() - t0;
    try { out.code = String((e && (e.type || e.error_message || e.message || e.code)) || e); }
    catch (x) { out.code = "ERR"; }
  }
  return out;
}
"""

# Map the login form: every input/button that is actually on the page, plus
# whether this repo's own selector candidates still match.
FORM_JS = r"""
(repoSelectors) => {
  const vis = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
    } catch (e) { return false; }
  };
  const describe = (el) => {
    const o = {
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute("type") || null,
      id: el.id || null,
      cls: (el.className && String(el.className).slice(0, 90)) || null,
      placeholder: el.getAttribute("placeholder") || null,
      name: el.getAttribute("name") || null,
      value_len: (el.value || "").length,
      text: (el.innerText || "").trim().slice(0, 60) || null,
      visible: vis(el),
      disabled: !!el.disabled,
      readonly: !!el.readOnly,
      contenteditable: el.getAttribute("contenteditable") || null,
      inputmode: el.getAttribute("inputmode") || null
    };
    try {
      // a stable-ish path, useful for writing a selector later
      const p = [];
      let n = el;
      for (let i = 0; i < 4 && n && n.tagName; i++) {
        p.unshift(n.tagName.toLowerCase() + (n.id ? "#" + n.id :
          (n.className ? "." + String(n.className).trim().split(/\s+/).slice(0, 2).join(".") : "")));
        n = n.parentElement;
      }
      o.path = p.join(" > ");
    } catch (e) {}
    return o;
  };
  const out = {};
  out.url = location.href;
  out.title = document.title;
  out.body_text = ((document.body && document.body.innerText) || "").slice(0, 600);
  out.inputs = Array.prototype.slice.call(
    document.querySelectorAll("input, [contenteditable='true']")).map(describe);
  out.buttons = Array.prototype.slice.call(
    document.querySelectorAll("button, [role='button'], .btn")).map(describe).slice(0, 25);
  out.auth_containers = ["#auth-pages", ".page-signIn", ".page-sign", ".tabs-container",
                         ".input-wrapper"].filter(s => {
    try { return !!document.querySelector(s); } catch (e) { return false; }
  });
  // do this repo's existing selector lists still match anything?
  out.repo_selectors = {};
  for (const key of Object.keys(repoSelectors || {})) {
    out.repo_selectors[key] = (repoSelectors[key] || []).map(sel => {
      let n = 0, visible = 0;
      try {
        const els = document.querySelectorAll(sel);
        n = els.length;
        els.forEach(e => { if (vis(e)) visible++; });
      } catch (e) { return { sel: sel, error: true }; }
      return { sel: sel, matches: n, visible: visible };
    }).filter(r => r.error || r.matches > 0);
  }
  return out;
}
"""


async def run(args) -> int:
    REPORT["args"] = vars(args)
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"login_ui_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    url = args.url or config.EITAA_WEB_URL
    light = False if args.no_light else None

    banner("SECTION 0  -  browser WITHOUT the recorder (this is the control)")
    info("the preflight ran with a recorder that patches MessagePort, Worker and")
    info("SharedWorker. Nothing is patched now, so if the calls behave the same,")
    info("the recorder is innocent; if they start working, it was the culprit.")
    t = time.time()
    try:
        session = await BrowserSession(args.account, headed=args.headed,
                                       light_assets=light).start()
    except Exception as exc:  # noqa: BLE001
        bad(f"browser failed to start - {exc}")
        check("browser starts", False, str(exc))
        return 2
    ok(f"browser up in {time.time() - t:.1f}s (no init script)")
    check("browser starts", True, "no instrumentation installed")
    page = session.page

    try:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            check("page loads", (resp.status if resp else 0) == 200,
                  f"HTTP {resp.status if resp else '?'}")
        except Exception as exc:  # noqa: BLE001
            check("page loads", False, str(exc))
            return 2
        await asyncio.sleep(float(args.wait))

        banner("SECTION 1  -  CONTROL: the same three calls, no instrumentation")
        await ev(page, f"() => {{ {RESOLVER_JS} }}", timeout=15)
        await ev(page, "(n) => { window.__MKWL_accountNumber = n; }", int(args.acc), timeout=10)
        src, _ = await ev(page, "() => ({resolved: !!window.__MKWL_api(), "
                                "source: window.__MKWL_apiSource})", timeout=15)
        info(f"resolver: {src}")
        calls = [
            {"label": "apiManager.getConfig()", "on": "api", "fn": "getConfig", "args": []},
            {"label": "apiManager.invokeApi('help.getConfig', {})", "on": "api",
             "fn": "invokeApi", "args": ["help.getConfig", {}]},
            {"label": "appStateManager.getState()", "on": "appStateManager",
             "fn": "getState", "args": []},
        ]
        answered = 0
        results = []
        for c in calls:
            c["timeout_ms"] = int(args.call_timeout * 1000)
            r, e = await ev(page, CALL_JS, c, timeout=args.call_timeout + 8)
            results.append({"call": c["label"], "result": r or {"evaluate_error": e}})
            if r and r.get("ok"):
                answered += 1
                ok(f"{c['label']} -> ANSWERED in {r['ms']}ms")
                info(f"    value: {str(r.get('value'))[:300]}")
            else:
                bad(f"{c['label']}: {(r or {}).get('code') or e}")
        REPORT["control_calls"] = results
        if answered:
            check("manager calls work without the recorder", True,
                  f"{answered}/{len(calls)} answered - the RECORDER was breaking them, "
                  f"not Eitaa. The API path is viable after all.")
        else:
            check("manager calls work without the recorder", False,
                  f"0/{len(calls)} answered with no instrumentation at all - the "
                  f"recorder is innocent; manager tasks genuinely do not resolve "
                  f"before a login")

        banner("SECTION 2  -  the login FORM, which needs none of the above")
        try:
            from eitaa import selectors as S
            repo_sel = {
                "SEARCH_INPUT (logged-in marker)": list(getattr(S, "SEARCH_INPUT", [])),
                "MESSAGE_INPUT": list(getattr(S, "MESSAGE_INPUT", [])),
                "SEND_BUTTON": list(getattr(S, "SEND_BUTTON", [])),
            }
        except Exception:  # noqa: BLE001
            repo_sel = {}
        form, err = await ev(page, FORM_JS, repo_sel, timeout=20)
        REPORT["form"] = form or {"error": err}
        if err or not form:
            bad(f"could not read the form - {err}")
            check("login form is present", False, str(err))
        else:
            info(f"url: {form['url']}   title: {form['title']!r}")
            info("page text:")
            for ln in str(form["body_text"]).splitlines()[:8]:
                if ln.strip():
                    print("        " + ln.strip()[:100], flush=True)
            dump("inputs / editable fields on the page:", form.get("inputs"))
            dump("buttons on the page:", form.get("buttons"))
            info(f"auth containers found: {form.get('auth_containers')}")
            if form.get("repo_selectors"):
                dump("which of this repo's own selectors still match:",
                     form.get("repo_selectors"))

            vis_inputs = [i for i in (form.get("inputs") or [])
                          if i.get("visible") and not i.get("disabled")]
            vis_buttons = [b for b in (form.get("buttons") or [])
                           if b.get("visible") and not b.get("disabled")]
            check("login form is present", bool(vis_inputs),
                  f"{len(vis_inputs)} usable input(s), {len(vis_buttons)} usable button(s)")
            phone_like = [i for i in vis_inputs
                          if (i.get("inputmode") in ("tel", "numeric")
                              or "tel" == (i.get("type") or "")
                              or "phone" in str(i.get("cls") or "").lower()
                              or "تلفن" in str(i.get("placeholder") or ""))]
            check("a phone-like field is identifiable", bool(phone_like),
                  json.dumps(phone_like[:2], ensure_ascii=False) if phone_like
                  else "none matched the phone heuristics - see the dump above")

        banner("SECTION 3  -  is the form actually drivable (types only, no click)")
        if not args.fill:
            info("skipped. Pass --fill <phone> to type a number into the field.")
            info("Even with --fill, the continue button is NOT clicked, so no code")
            info("is requested.")
            check("form accepts typed input", None, "not attempted (no --fill)")
        else:
            typed = None
            try:
                # Try the identified phone field first, then any visible text input.
                cands = ["input[inputmode='tel']", "input[type='tel']",
                         ".input-field-input", "input.input-field-input",
                         "#auth-pages input", "input[type='text']"]
                for sel in cands:
                    loc = page.locator(sel).first
                    try:
                        if await loc.is_visible():
                            await loc.click(timeout=4000)
                            await loc.fill("", timeout=4000)
                            await loc.type(str(args.fill), delay=60, timeout=10000)
                            val = await loc.input_value(timeout=4000)
                            typed = {"selector": sel, "value_after": val}
                            break
                    except Exception:  # noqa: BLE001
                        continue
            except Exception as exc:  # noqa: BLE001
                REPORT["fill_error"] = str(exc)
            REPORT["fill"] = typed
            if typed:
                ok(f"typed into {typed['selector']} -> field now reads "
                   f"{typed['value_after']!r}")
                check("form accepts typed input", True,
                      f"via {typed['selector']}; the continue button was deliberately "
                      f"NOT clicked")
                # after typing, the button usually becomes enabled - worth recording
                st, _ = await ev(page, r"""
                () => Array.prototype.slice.call(
                    document.querySelectorAll("button, [role='button']"))
                  .filter(b => b.offsetParent !== null)
                  .map(b => ({text: (b.innerText||'').trim().slice(0,40),
                              disabled: !!b.disabled,
                              cls: String(b.className||'').slice(0,60)})).slice(0, 10)
                """, timeout=10)
                dump("buttons after typing (an enabled 'continue' means the form is "
                     "ready to submit):", st)
            else:
                bad("could not type into any visible field")
                check("form accepts typed input", False, "no field accepted input")

            if args.i_want_to_click_continue:
                bad("--i-want-to-click-continue was passed: this WILL request a real "
                    "login code and can rate-limit the number. Clicking in 3s...")
                await asyncio.sleep(3)
                clicked = False
                for sel in ["button.btn-primary", "#auth-pages button", "button"]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible() and not await loc.is_disabled():
                            await loc.click(timeout=5000)
                            clicked = True
                            break
                    except Exception:  # noqa: BLE001
                        continue
                REPORT["clicked"] = clicked
                info(f"clicked: {clicked}; waiting 10s to see what the page does")
                await asyncio.sleep(10)
                after, _ = await ev(page, r"""
                () => ({text: ((document.body&&document.body.innerText)||'').slice(0,500),
                        inputs: document.querySelectorAll('input').length})
                """, timeout=10)
                dump("page after the click (a code-entry step means it worked):", after)

        try:
            await page.screenshot(path=str(outdir / "page.png"), full_page=True)
            (outdir / "page.html").write_text(await page.content(), encoding="utf-8")
            ok(f"screenshot + DOM saved to {outdir}")
        except Exception:  # noqa: BLE001
            pass
        if args.keep_open:
            await asyncio.sleep(float(args.keep_open))

        banner("RESULT")
        width = max(len(n) for n, _, _ in CHECKS) if CHECKS else 10
        for name, passed, detail in CHECKS:
            mark = "PASS" if passed is True else ("FAIL" if passed is False else "SKIP")
            print(f"  {mark}  {name.ljust(width)}  {detail}", flush=True)
        api_works = REPORT["checks"].get(
            "manager calls work without the recorder", {}).get("passed") is True
        form_ok = REPORT["checks"].get("login form is present", {}).get("passed") is True
        print("", flush=True)
        if api_works:
            REPORT["verdict"].append(
                "The recorder was the problem. Manager calls answer on a clean page, "
                "so the resolver fix is viable: swap window.apiManager for the "
                "resolver in the seven bridges and in login_flow's readiness check.")
            print("  The API route is alive once nothing is instrumenting it. Fix the\n"
                  "  bridges with the resolver and login should work.", flush=True)
        elif form_ok:
            REPORT["verdict"].append(
                "Manager calls do not resolve even on a clean page, but the login "
                "form is present and usable. Drive the form instead of the API: it "
                "does not depend on Eitaa's internal refactors.")
            print("  Take the front door: drive the login FORM. It survives Eitaa\n"
                  "  rewriting its internals, which the API bridges clearly do not.\n"
                  "  Next step: --fill to prove the field accepts input, then one real\n"
                  "  login where you receive the code and send it to the bot.", flush=True)
        else:
            REPORT["verdict"].append(
                "Neither the API route nor the form could be confirmed. Send this "
                "output plus the screenshot.")
            print("  Neither route confirmed - send the output and the screenshot in\n"
                  f"  {outdir}", flush=True)
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
        description="Control run without the recorder, plus a map of the login form. "
                    "Requests no login code unless you explicitly ask for a click.")
    p.add_argument("--account", required=True)
    p.add_argument("--wait", type=float, default=10)
    p.add_argument("--call-timeout", type=float, default=8)
    p.add_argument("--acc", type=int, default=1)
    p.add_argument("--fill", default=None,
                   help="type this phone into the form (never clicks continue)")
    p.add_argument("--i-want-to-click-continue", action="store_true",
                   help="DANGER: actually submits the form, which requests a REAL "
                        "login code and can rate-limit the number")
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
