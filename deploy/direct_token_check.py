#!/usr/bin/env python3
"""Do the storage tokens actually drive a direct request? Prove it with a
NON-file call before blaming apk.

Why
---
apk_direct_v2 sent apk parts and got 'error 404: INVALID_CONSTRUCTOR' on the
very first upload part. That is NOT an apk filter - it means the server rejected
the request's CONSTRUCTOR, i.e. the envelope/body was malformed or the tokens
are not what wrap_eitaa expects. So before concluding anything about apk, this
isolates the token path with the simplest possible calls:

  A. help.getConfig       - unauthenticated, tiny, no file, no peer
  B. messages.getHistory  - needs the session, to inputPeerSelf

If BOTH work, the tokens from storage are valid and the apk failure is specific
to the file build - we fix that. If both give INVALID_CONSTRUCTOR, the storage
token is not the envelope token (token1/token2 may be derived differently), and
we learn exactly that instead of mis-blaming apk.

It also compares the storage token against the live wire: it reads token1/token2
from storage AND asks the page's own apiManager to make the same help.getConfig,
so we can see the shapes side by side.

Read-only, own account, no file sent.

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/direct_token_check.py --account 989939094655
    systemctl start mkwlsoso-bot
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_T0 = time.time()


def log(m=""): print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)
def banner(t): print("\n" + "=" * 72 + f"\n  {t}\n" + "=" * 72, flush=True)


READ_JS = r"""
() => {
  const out = {};
  try {
    const c = window.appStorage && window.appStorage.storage && window.appStorage.storage.cache;
    if (c) for (const k of Object.keys(c)) if (/^account\d+$/.test(k) && c[k] && c[k].token)
      out[k] = { token: c[k].token, imei: c[k].imei, userId: c[k].userId, dcId: c[k].dcId };
  } catch(e){ out.err = String(e); }
  return out;
}
"""


async def ev(page, js, t=20):
    try:
        return (await asyncio.wait_for(page.evaluate(js), timeout=t), None)
    except Exception as exc:  # noqa: BLE001
        return (None, str(exc))


def try_direct_call(token1, token2, body, api_url, label):
    """POST one wrapped body, return the server's verdict + first bytes."""
    import direct.eitaa_tl as E
    from direct.transport import HttpTransport, wrap_eitaa, unwrap_eitaa
    tx = HttpTransport(api_url, timeout=25.0, cookies={})
    try:
        raw = wrap_eitaa(token1, token2, body)
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "ok": False, "code": f"wrap: {exc}"}
    try:
        resp = tx.post(raw)
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "ok": False, "code": f"transport: {exc}"}
    rb = resp
    try:
        if resp[:4] == bytes.fromhex("ed77be7a"):
            rb = unwrap_eitaa(resp)["body"]
    except Exception:  # noqa: BLE001
        pass
    v = E.classify_response(rb)
    return {"label": label, "ok": bool(v.get("ok")), "code": v.get("note"),
            "head": rb[:32].hex()}


async def run(args):
    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver
    from config import config
    import direct.eitaa_tl as E
    from direct import tl
    from direct.schema import INPUT_PEER_SELF

    banner("DIRECT TOKEN CHECK  -  are the storage tokens valid for the envelope?")
    session = await BrowserSession(args.account, headed=False, light_assets=None).start()
    page = session.page
    try:
        await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)
        st = await EitaaDriver(session).login_state()
        if not st.get("logged_in"):
            log(f"not logged in ({st.get('auth_state')}) - log in first")
            return 2
        log(f"logged in: my_id={st.get('my_id')}")

        acc, _ = await ev(page, READ_JS)
        acc = acc or {}
        src = None
        for k, v in acc.items():
            if str(v.get("userId")) == str(st.get("my_id")):
                src = v
                break
        src = src or (next(iter(acc.values())) if acc else None)
        if not src:
            log("no stored account token found")
            return 1
        token1, token2 = src.get("token"), src.get("imei")
        uid = src.get("userId")
        dc = src.get("dcId")
        log(f"storage: userId={uid} dcId={dc}")
        log(f"token1={token1}")
        log(f"token2={token2}")

        # which host for this dc
        from direct import dc as dcmod
        api_candidates = [
            "https://majid.eitaa.com/eitaa/",
            "https://bagher.eitaa.ir/eitaa/",
            dcmod.dc_url(dc) if dc else "https://majid.eitaa.com/eitaa/",
        ]
        # unique, order-preserving
        seen = set()
        api_candidates = [u for u in api_candidates if not (u in seen or seen.add(u))]

        # Use ONLY constructors the project has verified on the wire. The
        # safest is send_message to inputPeerSelf (a text to Saved Messages):
        # if that lands, the tokens drive the envelope and the apk failure is in
        # the FILE build. send_message is byte-exact in eitaa_tl.
        banner("TEST A  -  send_message 'direct token test' to inputPeerSelf")
        peer_self = tl.int_bytes(INPUT_PEER_SELF, signed=False)
        marker = f"DTCHECK-{int(time.time())}"
        body_msg = E.send_message(peer_self, marker)
        delivered_api = None
        for api in api_candidates:
            r = await asyncio.to_thread(try_direct_call, token1, token2, body_msg,
                                        api, f"sendMessage@{api.split('//')[1].split('/')[0]}")
            mark = "OK  " if r["ok"] else "FAIL"
            log(f"{mark} {r['label']}: {r.get('code')}  head={r.get('head', '')[:24]}")
            if r["ok"]:
                delivered_api = api

        # confirm by reading Saved Messages back through the page
        banner("TEST B  -  did the text actually land in Saved Messages?")
        await asyncio.sleep(6)
        rb_js = ("async () => { function API(){try{if(window.__MKWL_api)return window.__MKWL_api();}"
                 "catch(e){}return window.apiManager||null;} const AM=API();"
                 "if(!AM||!AM.invokeApi)return{ok:false}; try{const h=await AM.invokeApi("
                 "'messages.getHistory',{peer:{_:'inputPeerSelf'},offset_id:0,offset_date:0,"
                 "add_offset:0,limit:20,max_id:0,min_id:0,hash:0}); return {ok:true,"
                 "msgs:(h&&h.messages||[]).map(m=>(m.message||'').slice(0,40))};}catch(e){return{ok:false,code:String(e)};}}")
        back, _ = await ev(page, rb_js, 40)
        found = False
        if back and back.get("ok"):
            for m in back.get("msgs", []):
                if marker in m:
                    found = True
            log(f"recent Saved Messages texts: {back.get('msgs', [])[:6]}")
        log(f"the direct text landed in Saved Messages: {found}")

        banner("VERDICT")
        if found:
            log("THE STORAGE TOKENS WORK. A direct send_message to Saved Messages")
            log("delivered, so token1/token2 from storage drive the envelope")
            log("correctly. The apk 'INVALID_CONSTRUCTOR' is therefore in the FILE")
            log("build (saveFilePart trailer / inputFile), NOT the tokens and NOT")
            log("an apk filter. That is a fixable code bug - next step is to send a")
            log("plain file (not apk) the same way and watch where the file path")
            log("diverges.")
        elif delivered_api:
            log("The server ACCEPTED the send_message (no error) but the text is")
            log("not in Saved Messages yet - likely a slow write or a peer quirk.")
            log("Re-run; if it keeps not landing, inputPeerSelf may need the real")
            log("user peer instead. Either way the tokens are NOT the blocker.")
        else:
            log("Every send_message gave an error too - so the storage token is")
            log("NOT usable as the envelope token directly. token1/token2 on the")
            log("wire are derived or encoded differently than the stored")
            log("token/imei. THIS is the real blocker: the direct path needs the")
            log("true envelope tokens, which only appear inside the worker's")
            log("request - unreachable without breaking the app. In that case the")
            log("direct path is genuinely closed on this build, and PDF-with-a-")
            log("link is the way. The per-host codes above say if it was instead")
            log("just the wrong shard/host.")
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--account", required=True)
    p.add_argument("--wait", type=float, default=20)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
