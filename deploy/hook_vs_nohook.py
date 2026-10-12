#!/usr/bin/env python3
"""Does worker_capture.js break invokeApi? One call, with the hook and without.

In the arsenal run, help.getConfig / help.getNearestDc / users.getUsers ALL
timed out - yet the same help.getConfig answered in 113ms in apk_final the day
before. The one difference: the arsenal opens the browser WITH
eitaa/worker_capture.js as its init script (it needs the capture for the direct
context), while apk_final opened a clean browser.

worker_capture.js wraps Worker to record what the app sends to its MTProto
worker. If that wrapping interferes with the message channel, invokeApi would
never get its reply and would hang exactly as we saw - the same observer effect
a recorder caused earlier in this project.

This isolates it: open the SAME account twice, make the SAME call, once clean
and once with the hook, and time both. If clean answers and hooked hangs, the
hook is the culprit and the direct-context extraction needs a different approach
(load a saved capture, or capture without breaking the channel).

Read-only: one help.getConfig each, no code, no send. Own account only.

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/hook_vs_nohook.py --account 989939094655
    systemctl start mkwlsoso-bot
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def log(m=""):
    print(f"  {m}", flush=True)


CALL_JS = r"""
async () => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  const AM=API();
  if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi (resolver found nothing)"};
  const t=Date.now();
  try{
    const c=await Promise.race([
      AM.invokeApi("help.getConfig", {}),
      new Promise((_,j)=>setTimeout(()=>j(new Error("timeout 15s")),15000))
    ]);
    return {ok:true, ms:Date.now()-t, this_dc:c&&c.this_dc,
            dcs:(c&&c.dc_options||[]).length};
  }catch(e){ return {ok:false, ms:Date.now()-t, code:String(e)}; }
}
"""


async def one_run(account, use_hook, wait):
    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from config import config

    cap = os.path.join(_ROOT, "eitaa", "worker_capture.js")
    init = cap if use_hook else None
    tag = "WITH hook" if use_hook else "clean (no hook)"
    log(f"--- {tag} ---")
    t = time.time()
    session = await BrowserSession(account, headed=False, light_assets=None,
                                   init_script_path=init).start()
    page = session.page
    try:
        await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                        timeout=120_000)
        await asyncio.sleep(float(wait))
        await ensure_api_resolver(page)
        try:
            res = await asyncio.wait_for(page.evaluate(CALL_JS), timeout=25)
        except asyncio.TimeoutError:
            res = {"ok": False, "code": "python-side timeout 25s"}
        if res.get("ok"):
            log(f"  help.getConfig -> OK in {res['ms']}ms "
                f"(this_dc={res.get('this_dc')}, {res.get('dcs')} dc_options)")
        else:
            log(f"  help.getConfig -> FAILED: {res.get('code')} "
                f"(after {res.get('ms', '?')}ms)")
        return res
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
        log(f"  ({tag} total {time.time() - t:.1f}s)")


async def run(args):
    print("\n" + "=" * 70, flush=True)
    print("  worker_capture.js: does it break invokeApi?", flush=True)
    print("=" * 70, flush=True)
    log(f"account {args.account}; one help.getConfig each way\n")

    clean = await one_run(args.account, use_hook=False, wait=args.wait)
    print("", flush=True)
    hooked = await one_run(args.account, use_hook=True, wait=args.wait)

    print("\n" + "=" * 70, flush=True)
    print("  VERDICT", flush=True)
    print("=" * 70, flush=True)
    c_ok, h_ok = clean.get("ok"), hooked.get("ok")
    if c_ok and not h_ok:
        log("CONFIRMED: the hook breaks invokeApi. Clean answered, hooked hung.")
        log("The direct-context extraction cannot use this hook as-is. Options:")
        log("  - capture the envelope a different way (e.g. a CDP/network")
        log("    listener that observes without wrapping Worker), or")
        log("  - drive one real send in a CLEAN session and read the capture it")
        log("    leaves, then run the arsenal against that saved capture.")
    elif c_ok and h_ok:
        log("Both worked. The hook is NOT the problem; the earlier timeouts were")
        log("the session/network at that moment. Re-run the arsenal - it should")
        log("get past PHASE 1 now.")
    elif not c_ok and not h_ok:
        log("BOTH failed - so it is NOT the hook, it is this account's session or")
        log("the link right now. help.getConfig is the lightest call there is; if")
        log("it hangs clean too, the session is not actually serving API calls.")
        log(f"  clean:  {clean.get('code')}")
        log(f"  hooked: {hooked.get('code')}")
        log("Try deploy/net_health.py (is the link ok?) and a different account.")
    else:
        log("Odd: hooked worked but clean did not. Likely transient; re-run.")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--account", required=True)
    p.add_argument("--wait", type=float, default=20)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
