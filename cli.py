"""Command-line entry point for the Eitaa web capture tool.

Commands
--------
  login    Open the browser so you can log in manually. The session is saved
           in the account's isolated profile for later reuse.

  capture  Record one operation: idle baseline -> you perform the action in the
           visible browser -> short trail. Writes a capture run to artifacts/.

  analyze  Turn a capture run into a human-readable report.md.

  list     List existing capture runs.

  inspect  Print a safe, structural snapshot of the Eitaa Web DOM (to confirm
           or fix UI selectors). No message text or personal data is printed.

  send     Send ONE text message to a chat via the browser driver (a real
           end-to-end test of the send path).

This tool automates the OWNER'S OWN accounts only. It does not bypass login,
OTP, CAPTCHA, or rate limits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from config import config
from capture.analyzer import analyze
from capture.browser import open_session
from capture.recorder import RunRecorder
from pathlib import Path

import csv
import re

from eitaa.driver import (
    EitaaDriver, inspect_dom, inspect_menu, inspect_add_contact, inspect_attach, inspect_login,
)
from jobs.campaign import create_campaign, run_campaign, request_stop
from jobs.state import JobState
from capture import deep
from capture.deep import HOOKS_JS
from capture.dossier import build_dossier
from capture.extract_params import extract_run, summarize
from capture.bridge import (
    BRIDGE_JS, send_marker_to_saved, summarize_bridge, print_bridge_summary,
    bridge_send_test, print_bridge_send_summary,
)
from capture.bridge_file import run_file_test, print_file_test_summary
from capture.bridge import RESOLVE_PEERS_JS, print_reach_group


async def cmd_login(account: str) -> int:
    config.ensure_dirs()
    print(f"[login] opening browser for account '{account}'")
    print("[login] log in manually (phone + code). Do NOT share the code with anyone.")
    async with open_session(account) as session:
        await session.goto()
        print("[login] browser is open. Press ENTER here when you have finished logging in...")
        await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
        print("[login] session saved to profile:", session.profile_dir)
    return 0


async def _wait_enter(prompt: str) -> None:
    print(prompt)
    await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)


async def cmd_capture(account: str, op: str, manual: bool) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        await session.goto()
        rec = RunRecorder(session, op)
        await rec.start()
        print(f"[capture] run_id={rec.run_id}")
        print(f"[capture] marker for this run: {rec.marker}")
        print(f"[capture] recording idle baseline ({config.BASELINE_SECONDS}s), do nothing...")
        await rec.baseline()

        async def do_action() -> None:
            # Manual mode: the human performs exactly one operation in the
            # browser window, using the marker text where a message is needed.
            await _wait_enter(
                "[capture] PERFORM THE ACTION NOW in the browser "
                f"(use the marker text: {rec.marker}), then press ENTER..."
            )

        await rec.action(do_action if manual else None)
        run_dir = await rec.finish()
        print(f"[capture] done. artifacts: {run_dir}")
        print(f"[capture] next: python cli.py analyze --run {rec.run_id}")
    return 0


def cmd_analyze(run_id: str) -> int:
    out = analyze(run_id)
    print(f"[analyze] report written: {out}")
    print(out.read_text(encoding="utf-8"))
    return 0


async def cmd_probe(account: str, op: str, manual: bool) -> int:
    """Deep protocol capture: hooks + raw frames + assets + storage + dossier."""
    config.ensure_dirs()
    async with open_session(account, init_script_path=HOOKS_JS) as session:
        await session.goto()
        rec = RunRecorder(session, f"probe_{op}")
        await rec.start()
        print(f"[probe] run_id={rec.run_id}")
        print(f"[probe] marker: {rec.marker}")
        print(f"[probe] instrumentation injected (fetch/xhr/worker/wasm/crypto).")
        print(f"[probe] idle baseline ({config.BASELINE_SECONDS}s), do nothing...")
        await rec.baseline()
        await deep.pull_hooks(session.page, rec.emit_event)

        async def do_action() -> None:
            await _wait_enter(
                "[probe] PERFORM THE ACTION NOW in the browser "
                f"(login / open contacts / send, using marker {rec.marker} where text is needed), "
                "then press ENTER..."
            )

        await rec.action(do_action if manual else None)

        # Drain the in-page hook buffer (raw frames + crypto/worker/wasm records).
        n = await deep.pull_hooks(session.page, rec.emit_event)
        print(f"[probe] pulled {n} hook records")

        # Download JS/WASM assets and dump storage structure.
        urls = await deep.collect_asset_urls(session.page)
        assets_info = await deep.download_assets(session.context, urls, rec.run_dir / "assets")
        print(f"[probe] downloaded {assets_info['count']} assets -> {assets_info['dir']}")

        storage = await deep.dump_storage(session.page)
        (rec.run_dir / "storage.json").write_text(
            json.dumps(storage, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[probe] storage keys: localStorage={len(storage.get('localStorage_keys', []))} "
              f"indexeddb={len(storage.get('indexeddb', []))}")

        await rec.finish(extra_meta={"deep": True, "assets": assets_info})

    out = build_dossier(rec.run_id)
    print(f"[probe] dossier written: {out}")
    print(out.read_text(encoding="utf-8"))
    return 0


async def cmd_bridge(account: str, manual: bool) -> int:
    """Discover whether Eitaa Web exposes a directly-usable send bridge.

    Injects bridge.js, performs ONE controlled send of a unique marker to the
    owner's Saved Messages, then reports whether the marker (the message text)
    crossed a JS worker/port boundary in plaintext -- i.e. whether we can post
    a high-level send task to Eitaa's own engine instead of driving the UI.
    """
    import time as _time
    from capture import redactor

    config.ensure_dirs()
    async with open_session(account, init_script_path=BRIDGE_JS) as session:
        await session.goto()
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[bridge] not logged in. run: python cli.py login --account", account)
            return 2

        marker = f"MKWLBRIDGE{int(_time.time())}"
        try:
            await session.page.evaluate(
                "(m) => { if (window.__MKWLB_setMarker) window.__MKWLB_setMarker(m); }", marker
            )
        except Exception:  # noqa: BLE001
            pass

        # Structural scan of window globals for a directly-callable send method.
        try:
            probe = await session.page.evaluate(
                "() => window.__MKWLB_probe ? window.__MKWLB_probe() : []"
            )
        except Exception:  # noqa: BLE001
            probe = []

        # Clear anything buffered before the action so we isolate the send.
        try:
            await session.page.evaluate("() => window.__MKWLB_dump ? window.__MKWLB_dump() : []")
        except Exception:  # noqa: BLE001
            pass

        print(f"[bridge] marker: {marker}")
        if manual:
            await _wait_enter(
                "[bridge] SEND ONE MESSAGE NOW to your Saved Messages with EXACTLY this "
                f"text: {marker}\n[bridge] then press ENTER here..."
            )
            send_status = "manual"
        else:
            print("[bridge] sending the marker to your Saved Messages (safe)...")
            send_status = await send_marker_to_saved(driver, marker)
            print(f"[bridge] send status: {send_status}")

        # Let async worker traffic settle, then drain the instrumentation buffer.
        await session.page.wait_for_timeout(3000)
        try:
            records = await session.page.evaluate(
                "() => window.__MKWLB_dump ? window.__MKWLB_dump() : []"
            )
        except Exception:  # noqa: BLE001
            records = []

        summary = summarize_bridge(records or [], marker, probe or [])
        summary["send_status"] = send_status

        run_dir = config.ARTIFACTS_DIR / f"bridge_{account}_{int(_time.time())}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "bridge_records.json").write_text(
            json.dumps(redactor.redact_value(records or []), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "bridge_summary.json").write_text(
            json.dumps(redactor.redact_value(summary), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary["run_dir"] = str(run_dir)

        print_bridge_summary(summary)
    return 0


async def cmd_bridge_send(account: str) -> int:
    """Verify the discovered bridge can actually SEND (to Saved Messages only).

    Calls each bridge entry point (apiManager/apiManagerProxy.invokeApi and
    appMessagesManager.sendText) against the owner's own Saved Messages, then
    confirms which one truly delivered a message. This pins down the exact
    working call before we build the hybrid campaign sender.
    """
    config.ensure_dirs()
    import time as _time
    async with open_session(account) as session:
        await session.goto()
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[bridge-send] not logged in. run: python cli.py login --account", account)
            return 2

        marker = f"MKWLSEND{int(_time.time())}"
        print(f"[bridge-send] marker: {marker} (sending only to your Saved Messages)")
        result = await bridge_send_test(driver, marker)
        print_bridge_send_summary(result)
    return 0


async def cmd_bridge_real(account: str, peer: str | None, text: str | None) -> int:
    """Send ONE real message through the bridge (driver.bridge_send).

    With no --peer it defaults to the account's own Saved Messages (self id),
    so it is safe to run. Prints the method used and the server message id,
    verifying the exact fast-send path before trusting it in a campaign.
    """
    import time as _time
    config.ensure_dirs()
    text = text or f"MKWL bridge real test {int(_time.time())}"
    async with open_session(account) as session:
        await session.goto()
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[bridge-real] not logged in. run: python cli.py login --account", account)
            return 2

        if not peer:
            # Default to self (Saved Messages) so the test bothers nobody.
            try:
                peer = await session.page.evaluate(
                    """() => {
                        try { if (window.appPeersManager && window.appPeersManager.peerId != null)
                                return String(window.appPeersManager.peerId); } catch (e) {}
                        try { if (window.appImManager && window.appImManager.myId != null)
                                return String(window.appImManager.myId); } catch (e) {}
                        try { if (window.appUsersManager && window.appUsersManager.getSelf) {
                                const s = window.appUsersManager.getSelf();
                                if (s) return String(s.id != null ? s.id : s); } } catch (e) {}
                        return null;
                    }"""
                )
            except Exception:  # noqa: BLE001
                peer = None
            if not peer:
                print("[bridge-real] could not detect self id; pass --peer <peer_id>")
                return 2
            print(f"[bridge-real] no --peer given; defaulting to self (Saved Messages) id={peer}")

        res = await driver.bridge_send(peer, text)
        print(f"[bridge-real] peer   : {peer}")
        print(f"[bridge-real] result : {res}")
        if res.get("ok"):
            print(f"[bridge-real] ✅ SENT via {res.get('method')}  msg_id={res.get('msg_id')}")
        elif res.get("limit"):
            print(f"[bridge-real] 🚫 server limit: {res.get('code')}")
        else:
            print(f"[bridge-real] ❌ failed: {res.get('code')}  "
                  f"(invoke_err={res.get('invoke_err')})")
    return 0


async def cmd_bridge_file(account: str, file_path: str | None) -> int:
    """Investigate fast file sending (upload once + forward/reuse), safely.

    Uploads a file ONCE to the owner's Saved Messages, then tests forwarding it
    (drop_author) and re-using its uploaded document via sendMedia -- both send
    the file WITHOUT re-uploading. If no --file is given, a tiny temp file is
    created so it runs with zero setup.
    """
    import tempfile
    config.ensure_dirs()

    made_temp = False
    if not file_path:
        fd, file_path = tempfile.mkstemp(prefix="mkwl_filetest_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("MKWL file-bridge test file. Safe to delete.\n" * 20)
        made_temp = True
        print(f"[bridge-file] no --file given; created a tiny test file: {file_path}")

    try:
        async with open_session(account) as session:
            await session.goto()
            driver = EitaaDriver(session)
            await driver.open()
            if not await driver.is_logged_in():
                print("[bridge-file] not logged in. run: python cli.py login --account", account)
                return 2
            print(f"[bridge-file] uploading once to Saved Messages, then testing forward + reuse...")
            res = await run_file_test(driver, file_path)
            print_file_test_summary(res)
    finally:
        if made_temp:
            try:
                os.remove(file_path)
            except Exception:  # noqa: BLE001
                pass
    return 0


async def cmd_bridge_login(account: str, phone: str) -> int:
    """SAFE bridge login test: no noVNC, phone + code entered here.

    Requests ONE login code via the bridge (never retries -> no rate-limit
    risk), signs in with the code you type, then reloads to confirm Eitaa Web
    recognizes the session. Detects 2FA. Uses this account's own isolated
    profile, so it is safe to add many accounts (each a different phone).
    """
    from eitaa.login_flow import (
        normalize_phone_intl, resolve_api_creds, send_code, sign_in,
    )

    config.ensure_dirs()
    intl = normalize_phone_intl(phone)
    api_id, api_hash = resolve_api_creds()

    async with open_session(account) as session:
        await session.goto()
        driver = EitaaDriver(session)
        await driver.open()

        if await driver.is_logged_in():
            print(f"[login] account '{account}' is ALREADY logged in. Nothing to do.")
            return 0

        print(f"[login] account   : {account}")
        print(f"[login] profile    : {config.profile_dir(account)}  (isolated)")
        print(f"[login] phone      : {intl}")
        print("[login] requesting ONE login code via the bridge (no retries)...")

        sc = await send_code(driver, intl, api_id, api_hash)
        if not sc.get("ok"):
            code = str(sc.get("code", ""))
            if "FLOOD" in code.upper():
                print(f"[login] 🚫 RATE LIMITED by the server: {code}")
                print("[login]   DO NOT retry now. Wait the stated time, then try again.")
            else:
                print(f"[login] ❌ sendCode failed: {code}")
            return 0
        phch = sc.get("phone_code_hash")
        if not phch:
            print("[login] ❌ no phone_code_hash returned; cannot continue.")
            return 0
        print(f"[login] ✅ code sent (type={sc.get('type')}). Check your Eitaa app / SMS.")

        print("[login] enter the code you received, then press ENTER:")
        code = (await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)).strip()
        code = re.sub(r"\D", "", code)
        if not code:
            print("[login] no code entered; aborting (nothing re-sent).")
            return 0

        si = await sign_in(driver, intl, phch, code)
        if si.get("needs_password"):
            print("[login] 🔐 this account has 2FA (a login password).")
            print("[login]   2FA via the bridge (SRP) isn't wired yet. For THIS account,")
            print("[login]   use the noVNC login once, or ask me to build 2FA next.")
            return 0
        if not si.get("ok"):
            print(f"[login] ❌ signIn failed: {si.get('code')}")
            return 0

        print(f"[login] signIn OK (result={si.get('result')})")
        print(f"[login] finalize steps : {si.get('finalize')}")
        print(f"[login] auth probe     : {si.get('probe')}")

        # First see if tweb switched to logged-in state LIVE (no reload).
        await driver.page.wait_for_timeout(1500)
        if await driver.is_logged_in():
            print("[login] ✅✅ LOGGED IN via the bridge (recognized live, no reload). "
                  "Profile saved -- no noVNC needed.")
            return 0

        print("[login] not logged-in live; reloading so Eitaa Web reloads the saved session...")
        try:
            await driver.page.reload(wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass
        await driver.page.wait_for_timeout(6000)

        if await driver.is_logged_in():
            print("[login] ✅✅ LOGGED IN via the bridge (after reload). "
                  "Profile saved -- no noVNC needed.")
        else:
            print("[login] ⚠️ signIn + finalize done, but the UI still isn't logged-in.")
            print("[login]   Paste the 'auth probe' + 'finalize steps' lines above so we can")
            print("[login]   call the exact state method this Eitaa build uses.")
    return 0


async def cmd_bridge_file_send(account: str, peer: str | None, file_path: str | None) -> int:
    """End-to-end test of the PRODUCTION file path (upload once + reuse-send).

    Uploads the file once via the bridge, then sends it once via sendMedia
    reuse. Defaults the target to the account's own Saved Messages (self), so
    it is safe. Verifies the exact functions the campaign uses.
    """
    import tempfile
    config.ensure_dirs()

    made_temp = False
    if not file_path:
        fd, file_path = tempfile.mkstemp(prefix="mkwl_filesend_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("MKWL production file-send test. Safe to delete.\n" * 20)
        made_temp = True
        print(f"[file-send] no --file given; created a tiny test file: {file_path}")

    try:
        async with open_session(account) as session:
            await session.goto()
            driver = EitaaDriver(session)
            await driver.open()
            if not await driver.is_logged_in():
                print("[file-send] not logged in. run: python cli.py login --account", account)
                return 2

            print("[file-send] uploading file ONCE via the bridge...")
            finit = await driver.bridge_file_init(file_path, "")
            print(f"[file-send] init: {finit}")
            if not finit.get("ok"):
                print(f"[file-send] ❌ upload/init failed: {finit.get('code')}")
                return 0

            if not peer:
                try:
                    peer = await session.page.evaluate(
                        """() => {
                            try { if (window.appPeersManager && window.appPeersManager.peerId != null)
                                    return String(window.appPeersManager.peerId); } catch (e) {}
                            try { if (window.appImManager && window.appImManager.myId != null)
                                    return String(window.appImManager.myId); } catch (e) {}
                            return null;
                        }"""
                    )
                except Exception:  # noqa: BLE001
                    peer = None
                if not peer:
                    print("[file-send] could not detect self id; pass --peer <peer_id>")
                    return 2
                print(f"[file-send] no --peer given; defaulting to self (Saved Messages) id={peer}")

            res = await driver.bridge_file_send(peer, "MKWL file bridge test caption")
            print(f"[file-send] send result: {res}")
            if res.get("ok"):
                print(f"[file-send] ✅ FILE SENT via {res.get('method')}  msg_id={res.get('msg_id')} "
                      f"(no re-upload)")
            elif res.get("limit"):
                print(f"[file-send] 🚫 server limit: {res.get('code')}")
            else:
                print(f"[file-send] ❌ failed: {res.get('code')}")
    finally:
        if made_temp:
            try:
                os.remove(file_path)
            except Exception:  # noqa: BLE001
                pass
    return 0


def _head_str(v) -> str:
    """Normalize a captured head value (may be a hex string or a {hex,text} dict)."""
    if isinstance(v, dict):
        return v.get("hex") or (("text:" + v["text"]) if v.get("text") else "")
    return v or ""


def _decode_eitaa_envelope(hexstr: str):
    """Decode Eitaa's transport framing from a captured request head.

    Observed shape (all requests share it):
        ed77be7a            4-byte constant magic
        <1 byte>            length N of the ASCII routing token
        <N ASCII bytes>     token, e.g. "9179.c756a2d10f.e41c4e_<userid>"
        <rest>              the actual MTProto payload (auth_key_id | msg_key | enc)

    Returns a human summary, or None if the head does not match.
    """
    if not hexstr or not isinstance(hexstr, str):
        return None
    hexstr = hexstr.strip().lower()
    if not hexstr.startswith("ed77be7a"):
        return None
    try:
        raw = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(raw) < 6:
        return None
    tok_len = raw[4]
    token = raw[5:5 + tok_len]
    after = raw[5 + tok_len:]
    try:
        token_txt = token.decode("ascii")
    except UnicodeDecodeError:
        token_txt = "<non-ascii token>"
    have_full_token = len(token) == tok_len
    akid = after[:8].hex() if len(after) >= 8 else after.hex()
    return (
        f"magic=ed77be7a tokenLen={tok_len} "
        f"token={token_txt!r}{'' if have_full_token else ' (TRUNCATED)'} "
        f"| after-token first8={akid}"
    )


def cmd_direct_replay(account: str, index=None) -> int:
    """PROVE the browser-free transport end-to-end with ZERO body guessing.

    Loads the newest worker-capture for the account, picks ONE small idempotent
    API request (Eitaa's config request is a 4-byte body -> smallest total), and
    resends its EXACT captured bytes to the SAME URL straight from Python via
    direct/transport.HttpTransport. If Eitaa returns a valid reply (the DC/config
    list contains "eitaa" hostnames, or any TL-looking bytes), then our HTTPS
    transport + the ed77be7a envelope + the session token all work headless.

    This burns at most ONE idempotent request (no message is sent).
    """
    import glob as _glob
    from pathlib import Path as _Path
    from direct.transport import HttpTransport, unwrap_eitaa, wrap_eitaa

    cap_dir = config.ARTIFACTS_DIR / "sessions"
    pattern = str(cap_dir / f"worker_tx_{account}_*.json")
    files = sorted(_glob.glob(pattern))
    if not files:
        print(f"[replay] no worker capture found. first run:")
        print(f"[replay]   DISPLAY=:99 python cli.py direct-capture-worker --account {account}")
        return 1
    cap_path = files[-1]
    print(f"[replay] using capture: {cap_path}")
    try:
        recs = json.loads(_Path(cap_path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[replay] cannot read capture: {exc}")
        return 1

    # Build the list of REPLAYABLE api requests: fetch to a /eitaa/ endpoint,
    # NOT a media/image response, and FULLY captured (reqHead holds every byte).
    def _is_media(res_hex: str) -> bool:
        return res_hex[:6] == "ffd8ff" or res_hex[:8] == "89504e47"

    candidates = []
    for i, r in enumerate(recs):
        if r.get("kind") not in ("fetch", "xhr"):
            continue
        url = r.get("url") or ""
        if "/eitaa/" not in url:
            continue
        req_hex = _head_str(r.get("reqHead"))
        res_hex = _head_str(r.get("resHead"))
        if not req_hex or not req_hex.startswith("ed77be7a"):
            continue
        if _is_media(res_hex):
            continue
        req_len = int(r.get("reqLen") or 0)
        if req_len == 0 or (len(req_hex) // 2) < req_len:
            continue  # request body was truncated -> cannot replay exactly
        candidates.append((i, req_len, url, req_hex))

    if not candidates:
        print("[replay] no fully-captured, non-media API request to replay.")
        print("[replay] re-run direct-capture-worker (need the small config request).")
        return 2

    if index is not None:
        chosen = next((c for c in candidates if c[0] == index), None)
        if not chosen:
            print(f"[replay] index {index} is not a replayable API request. options:")
            for i, ln, url, _ in candidates:
                print(f"[replay]   #{i}  {ln}B  {url}")
            return 2
    else:
        # smallest request => Eitaa's idempotent config (safest to replay)
        chosen = min(candidates, key=lambda c: c[1])

    idx, req_len, url, req_hex = chosen
    raw = bytes.fromhex(req_hex)[:req_len]

    parsed = unwrap_eitaa(raw)
    # Offline self-check: our wrap_eitaa must reproduce the real bytes exactly.
    rebuilt = wrap_eitaa(parsed["token1"], parsed["token2"], parsed["body"])
    wrap_ok = rebuilt == raw
    print(f"[replay] record #{idx}  {req_len}B  -> {url}")
    print(f"[replay] token2 (session id): {parsed['token2']}")
    print(f"[replay] body ({len(parsed['body'])}B): {parsed['body'].hex()}")
    print(f"[replay] wrap_eitaa reproduces captured bytes exactly: "
          f"{'YES' if wrap_ok else 'NO (envelope model wrong!)'}")

    print("[replay] sending the SAME bytes from pure Python (browser closed)...")
    try:
        resp = HttpTransport(url, timeout=30.0).post(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"[replay] ✗ transport error: {exc}")
        print("[replay] (a 4xx/5xx or TLS error still tells us the host answered)")
        return 3

    head = resp[:80].hex()
    print(f"[replay] ✓ HTTP 200, response {len(resp)}B  head={head}")
    # Heuristics: did we get a real, meaningful reply?
    txt = resp.decode("latin-1", "ignore")
    looks_config = "eitaa" in txt
    looks_gzip = resp[:3] == b"\x1f\x8b\x08"
    looks_vector = b"\x1c\xb5\xc4\x15" in resp[:64]  # 15c4b51c LE vector id
    if looks_config:
        hosts = sorted({w for w in txt.replace("\x00", " ").split()
                        if "eitaa" in w and "." in w})[:8]
        print(f"[replay] 🎉 CONFIG REPLY — contains eitaa hosts: {hosts}")
        print("[replay] TRANSPORT PROVEN: headless Python talks to Eitaa via the envelope.")
    elif looks_gzip:
        print("[replay] reply is gzip (a real TL payload) — transport works; "
              "will gunzip+parse in the client step.")
    elif looks_vector:
        print("[replay] reply starts with a TL Vector — transport works.")
    else:
        print("[replay] reply received (not obviously config). Paste this head to me "
              "and I'll decode the response framing.")
    return 0


def _print_op_capture(label: str, recs: list) -> list:
    """Pretty-print the API requests captured for one operation and return the
    replayable ones (fully-captured, non-media, /eitaa/ POSTs)."""
    def _is_media(res_hex: str) -> bool:
        return res_hex[:6] == "ffd8ff" or res_hex[:8] == "89504e47"

    api = []
    for i, r in enumerate(recs):
        if r.get("kind") not in ("fetch", "xhr"):
            continue
        url = r.get("url") or ""
        if "/eitaa/" not in url:
            continue
        req_hex = _head_str(r.get("reqHead"))
        res_hex = _head_str(r.get("resHead"))
        if not req_hex.startswith("ed77be7a") or _is_media(res_hex):
            continue
        api.append((i, r, req_hex, res_hex))

    print(f"\n[capall] ===== OP: {label} =====  ({len(api)} API request(s))")
    if not api:
        print("[capall]   (no API request captured for this op)")
        return []
    # The method call is almost always the LARGEST new request in the batch.
    api_sorted = sorted(api, key=lambda t: -(t[1].get("reqLen") or 0))
    for rank, (i, r, req_hex, res_hex) in enumerate(api_sorted):
        raw = bytes.fromhex(req_hex)
        # Strip envelope to isolate the TL body we must learn to build.
        body_hex = ""
        try:
            p = 4
            l1 = raw[p]; p += 1 + l1
            l2 = raw[p]; p += 1 + l2
            blen = int.from_bytes(raw[p:p + 4], "big"); p += 4
            body_hex = raw[p:p + blen].hex()
        except Exception:  # noqa: BLE001
            body_hex = "(parse error)"
        tag = "  <== LIKELY THE METHOD CALL" if rank == 0 else ""
        print(f"[capall]  req#{i}  {r.get('reqLen')}B{tag}")
        print(f"[capall]     body({len(body_hex)//2}B)={body_hex}")
        print(f"[capall]     res {r.get('resLen')}B head={res_hex[:96]}")
    return [t[1] for t in api]


def _newest_capture(account: str):
    """Return (path, loaded_json) for the newest capall_/worker_tx_ capture."""
    import glob as _glob
    import os as _os
    from pathlib import Path as _Path
    cap_dir = config.ARTIFACTS_DIR / "sessions"
    files = (
        _glob.glob(str(cap_dir / f"capall_{account}_*.json"))
        + _glob.glob(str(cap_dir / f"worker_tx_{account}_*.json"))
    )
    if not files:
        return None, None
    # newest by real modification time (not alphabetical: 'capall' < 'worker_tx')
    path = max(files, key=_os.path.getmtime)
    return path, json.loads(_Path(path).read_text(encoding="utf-8"))


def _direct_rpc(body: bytes, ctx: dict, url: str, label: str) -> int:
    """Wrap a bare TL body in the Eitaa envelope, POST it browser-free, and
    classify the response. Shared by direct-send / direct-import."""
    from direct.transport import HttpTransport, wrap_eitaa, unwrap_eitaa
    from direct import eitaa_tl as E

    raw = wrap_eitaa(ctx["token1"], ctx["token2"], body)
    print(f"[{label}] request {len(raw)}B -> {url}")
    print(f"[{label}] body({len(body)}B)={body.hex()}")
    try:
        resp = HttpTransport(url, timeout=30.0).post(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] ✗ transport error: {exc}")
        return 3
    # Eitaa responses come back wrapped in the same envelope shape.
    resp_body = resp
    try:
        if resp[:4] == bytes.fromhex("ed77be7a"):
            resp_body = unwrap_eitaa(resp)["body"]
    except Exception:  # noqa: BLE001
        pass
    print(f"[{label}] ✓ HTTP 200, response {len(resp)}B  head={resp[:64].hex()}")
    verdict = E.classify_response(resp_body)
    if verdict["ok"]:
        print(f"[{label}] 🎉 SUCCESS — {verdict['note']} (browser-free path works)")
        return 0
    print(f"[{label}] ⚠ server did not accept it — {verdict['note']}")
    print(f"[{label}] paste this response head to me: {resp_body[:80].hex()}")
    return 4


def cmd_direct_send(account: str, text: str, url: str | None) -> int:
    """Send a TEXT message to Saved Messages with NO browser (direct MTProto)."""
    from direct import eitaa_tl as E
    path, cap = _newest_capture(account)
    if not cap:
        print(f"[dsend] no capture for '{account}'. first run:")
        print(f"[dsend]   DISPLAY=:99 python cli.py direct-capture-all --account {account}")
        return 1
    print(f"[dsend] session context from: {path}")
    try:
        ctx = E.extract_context(cap)
    except Exception as exc:  # noqa: BLE001
        print(f"[dsend] cannot extract session context: {exc}")
        return 2
    if not ctx.get("self_peer"):
        print("[dsend] self peer not found in capture (need a request that targets Saved Messages).")
        return 2
    print(f"[dsend] user_id={ctx['user_id']}  token2={ctx['token2']}")
    body = E.send_message(ctx["self_peer"], text)
    endpoint = url or "https://majid.eitaa.com/eitaa/"
    return _direct_rpc(body, ctx, endpoint, "dsend")


def cmd_direct_import(account: str, phone: str, first: str, last: str, url: str | None) -> int:
    """Import ONE contact with NO browser (direct MTProto)."""
    from direct import eitaa_tl as E
    path, cap = _newest_capture(account)
    if not cap:
        print(f"[dimport] no capture for '{account}'. first run direct-capture-all.")
        return 1
    print(f"[dimport] session context from: {path}")
    try:
        ctx = E.extract_context(cap)
    except Exception as exc:  # noqa: BLE001
        print(f"[dimport] cannot extract session context: {exc}")
        return 2
    body = E.import_contacts([(phone, first, last)])
    endpoint = url or "https://majid.eitaa.com/eitaa/"
    return _direct_rpc(body, ctx, endpoint, "dimport")


async def cmd_direct_capture_all(account: str, phone: str, first: str) -> int:
    """Capture the wire bytes for ALL current bot operations in ONE browser run:
    send text, send file (document), and add/import a contact.

    Each operation's worker traffic is drained separately and labelled, so we
    can reverse each TL method (sendMessage / sendMedia+upload / importContacts)
    and rebuild them browser-free in direct/.
    The contact uses a throwaway test number (default is an obviously-fake,
    unassigned MSISDN) so no real person is touched; override with --phone.
    """
    import time as _time
    import tempfile as _tf
    from pathlib import Path as _Path
    from capture.bridge import send_marker_to_saved
    config.ensure_dirs()

    init_js = _Path("eitaa/worker_capture.js")
    if not init_js.is_file():
        print(f"[capall] missing {init_js}")
        return 1

    all_labeled: dict = {}
    async with open_session(account, init_script_path=str(init_js)) as session:
        await session.goto()
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[capall] not logged in. run: python cli.py login --account", account)
            return 2

        await driver.dump_worker_requests()  # clear anything buffered pre-action

        # ---- 1) SEND TEXT -------------------------------------------------
        marker = f"MKWLTX{int(_time.time())}"
        print(f"[capall] (1/3) sending TEXT to Saved Messages: {marker}")
        st = await send_marker_to_saved(driver, marker)
        print(f"[capall]      text status: {st}")
        await session.page.wait_for_timeout(3500)
        all_labeled["text"] = await driver.dump_worker_requests()

        # ---- 2) SEND FILE (document) -------------------------------------
        tmp = _Path(_tf.gettempdir()) / f"mkwl_capall_{int(_time.time())}.txt"
        tmp.write_text(f"mkwlsoso capture-all file test {marker}\n", encoding="utf-8")
        print(f"[capall] (2/3) sending FILE to Saved Messages: {tmp.name}")
        try:
            fr = await driver.send_file(str(tmp), caption=f"cap {marker}", to_saved=True)
            print(f"[capall]      file status: ok={getattr(fr, 'ok', '?')} detail={getattr(fr, 'detail', '')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[capall]      file send raised: {exc}")
        await session.page.wait_for_timeout(4500)
        all_labeled["file"] = await driver.dump_worker_requests()

        # ---- 3) ADD / IMPORT CONTACT -------------------------------------
        print(f"[capall] (3/3) adding CONTACT (test number {phone})")
        try:
            cr = await driver.add_contacts_batch([{"phone": phone, "first": first, "last": ""}])
            print(f"[capall]      contact result: {cr}")
        except Exception as exc:  # noqa: BLE001
            print(f"[capall]      add-contact raised: {exc}")
        await session.page.wait_for_timeout(3500)
        all_labeled["contact"] = await driver.dump_worker_requests()

        try:
            tmp.unlink()
        except Exception:  # noqa: BLE001
            pass

    # Persist everything (gitignored artifacts) for offline reversing.
    out_dir = config.ARTIFACTS_DIR / "sessions"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"capall_{account}_{int(_time.time())}.json"
    out_path.write_text(json.dumps(all_labeled, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n[capall] ================= SUMMARY =================")
    total = sum(len(v) for v in all_labeled.values())
    print(f"[capall] total worker records: {total}   saved: {out_path}")
    for label in ("text", "file", "contact"):
        _print_op_capture(label, all_labeled.get(label, []))
    print("\n[capall] ==========================================")
    print("[capall] Send me the 3 OP sections above (esp. the 'LIKELY THE METHOD CALL'")
    print("[capall] body of each). I'll reverse sendMessage / sendMedia / importContacts")
    print("[capall] and build them into the browser-free direct client.")
    return 0


async def cmd_direct_capture_worker(account: str) -> int:
    """Capture the EXACT bytes Eitaa's MTProto Worker sends on the wire.

    Opens the session with worker_capture.js injected as an init script (so it
    wraps window.Worker BEFORE the mtproto worker is created), performs a
    controlled send to Saved Messages, then dumps the worker's fetch/XHR/WS
    requests + responses (hex heads). This reveals Eitaa's transport envelope.
    """
    import time as _time
    from pathlib import Path as _Path
    from capture.bridge import send_marker_to_saved
    config.ensure_dirs()

    init_js = _Path("eitaa/worker_capture.js")
    if not init_js.is_file():
        print(f"[wcap] missing {init_js}")
        return 1

    async with open_session(account, init_script_path=str(init_js)) as session:
        await session.goto()
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[wcap] not logged in. run: python cli.py login --account", account)
            return 2

        await driver.dump_worker_requests()  # clear anything buffered pre-action
        marker = f"MKWLW{int(_time.time())}"
        print("[wcap] performing a controlled send to trigger MTProto worker traffic...")
        status = await send_marker_to_saved(driver, marker)
        print(f"[wcap] send status: {status}")
        await session.page.wait_for_timeout(4000)

        recs = await driver.dump_worker_requests()
        out_dir = config.ARTIFACTS_DIR / "sessions"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"worker_tx_{account}_{int(_time.time())}.json"
        out_path.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")

        print("[wcap] ===== WORKER TRANSPORT CAPTURE =====")
        print(f"[wcap] records: {len(recs)}   saved: {out_path}")
        kinds: dict = {}
        for r in recs:
            kinds[r.get("kind", "?")] = kinds.get(r.get("kind", "?"), 0) + 1
        print(f"[wcap] by kind: {kinds}")
        # Show the interesting ones: WS frames + binary POSTs (not media).
        shown = 0
        for r in recs:
            k = r.get("kind")
            if k in ("no_hook",):
                print(f"[wcap] {r.get('note')}")
                continue
            if k in ("ws_open",):
                print(f"[wcap] WS OPEN {r.get('url')}")
                shown += 1
            elif k in ("ws_send", "ws_recv"):
                head = _head_str(r.get("reqHead")) or _head_str(r.get("resHead")) or r.get("resText") or ""
                print(f"[wcap] {k} {r.get('reqLen') or r.get('resLen') or 0}B  {str(head)[:64]}")
                shown += 1
            elif k in ("fetch", "xhr"):
                req = _head_str(r.get("reqHead"))
                res = _head_str(r.get("resHead"))
                # skip obvious media (jpeg/png responses) to reduce noise
                is_img = res[:6] == "ffd8ff" or res[:8] == "89504e47"
                tag = " [media]" if is_img else ""
                print(f"[wcap] {k}{tag} {r.get('url')}")
                print(f"[wcap]    req {r.get('reqLen', 0)}B head={req}")
                print(f"[wcap]    res {r.get('resLen', 0)}B head={res}")
                dec = _decode_eitaa_envelope(req)
                if dec:
                    print(f"[wcap]    envelope: {dec}")
                shown += 1
            if shown >= 25:
                break
        print("[wcap] ====================================")
        print("[wcap] Send me these lines. WS frames or a non-media binary POST reveal the")
        print("[wcap] envelope: the FIRST send is usually a 64-byte obfuscation init header.")
    return 0


async def cmd_direct_capture_transport(account: str) -> int:
    """Pin Eitaa's real MTProto wire: URL per DC + transport envelope.

    Installs a fetch/XHR probe, performs a controlled send to Saved Messages,
    then reports the binary requests (URL + first bytes of request/response) so
    the direct client's transport (direct/dc.py) can be set to the real URL and
    we can confirm the body is raw MTProto with no extra framing.
    """
    import time as _time
    from capture.bridge import send_marker_to_saved
    config.ensure_dirs()
    async with open_session(account) as session:
        await session.goto()
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[tx] not logged in. run: python cli.py login --account", account)
            return 2
        if not await driver.inject_transport_probe():
            print("[tx] could not inject transport probe.")
            return 1
        await driver.dump_transport()  # clear anything buffered pre-action

        marker = f"MKWLTX{int(_time.time())}"
        print("[tx] performing a controlled send to Saved Messages to trigger traffic...")
        status = await send_marker_to_saved(driver, marker)
        print(f"[tx] send status: {status}")
        await session.page.wait_for_timeout(3500)

        recs = await driver.dump_transport()
        # De-dup by URL, keep the binary (MTProto) ones first.
        seen_urls = {}
        for r in recs:
            u = r.get("url", "")
            if u and u not in seen_urls:
                seen_urls[u] = r
        print("[tx] ===== TRANSPORT CAPTURE =====")
        print(f"[tx] binary/MTProto requests captured: {len(recs)}")
        for u, r in list(seen_urls.items())[:12]:
            print(f"[tx] {r.get('via')} {r.get('method')} {u}")
            print(f"[tx]    req {r.get('reqLen')}B head={ (r.get('reqHead') or '')[:48] }")
            print(f"[tx]    res {r.get('respLen')}B head={ (r.get('respHead') or '')[:48] }")
        print("[tx] =============================")
        print("[tx] Send me these lines. The URL(s) pin the DC endpoint; the req head's")
        print("[tx] first 8 bytes should be the auth_key_id (confirms raw MTProto over HTTP).")
    return 0


async def cmd_bridge_export_session(account: str) -> int:
    """Export the browser profile's MTProto session for the direct client.

    Saves the full raw export to a gitignored artifacts file and prints a
    REDACTED summary (hints + shapes only, never the auth_key bytes) so we can
    pin the exact tweb storage keys and then load it in the direct client.
    """
    import time as _time
    config.ensure_dirs()
    async with open_session(account) as session:
        await session.goto()
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[export] not logged in. run: python cli.py login --account", account)
            return 2

        exp = await driver.export_session()
        if not exp:
            print("[export] could not export session (bridge unavailable).")
            return 1

        out_dir = config.ARTIFACTS_DIR / "sessions"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{account}_{int(_time.time())}.json"
        out_path.write_text(json.dumps(exp, ensure_ascii=False, indent=2), encoding="utf-8")

        hints = exp.get("hints") or {}
        idb = exp.get("indexeddb") or {}
        print("[export] ===== SESSION EXPORT =====")
        print(f"[export] saved (full, gitignored): {out_path}")
        print(f"[export] localStorage keys : {sorted((exp.get('localStorage') or {}).keys())}")
        # Non-bulk stores: show their KEY names (this is where auth lives).
        for name, db in idb.items():
            stores = db.get("stores") or {}
            skipped = db.get("skipped") or {}
            for sname, entries in stores.items():
                keys = sorted(entries.keys())
                if keys:
                    print(f"[export] idb {name}/{sname} keys: {keys[:40]}")
            if skipped:
                print(f"[export] idb {name} skipped(bulk): "
                      + ", ".join(f"{k}={v}" for k, v in skipped.items()))
        print(f"[export] auth_key candidates: {hints.get('auth_keys') or 'NONE FOUND'}")
        print(f"[export] server_salt (8B)   : {hints.get('salts') or 'none'}")
        print(f"[export] user id paths      : {hints.get('user_ids') or 'none'}")
        print(f"[export] dc hints           : {hints.get('dcs') or 'none'}")
        print("[export] ==========================")
        print("[export] Send me the lines above (they contain NO secret bytes) so I can")
        print("[export] finalize the direct-client session loader against your real keys.")
    return 0


async def cmd_bridge_reach(account: str, sample: int) -> int:
    """Measure how far the fast bridge reaches: PVs vs Contacts.

    Collects the account's private chats (PVs) and its Contacts, then asks
    Eitaa's own peer manager whether each peer_id resolves to a real inputPeer
    (with access_hash). NOTHING is sent -- this only reports which group the
    fast direct send (invokeApi) can hit vs which would use the fallback.
    """
    config.ensure_dirs()
    async with open_session(account) as session:
        await session.goto()
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[reach] not logged in. run: python cli.py login --account", account)
            return 2

        print("[reach] collecting PVs (private chats)...")
        chats = await driver.collect_all_chats()
        pvs = [c for c in chats if c.get("kind") == "user" and c.get("peer_id")]
        print(f"[reach] PVs found: {len(pvs)}")

        print("[reach] collecting Contacts...")
        try:
            contacts = await driver.collect_all_contacts()
        except Exception as exc:  # noqa: BLE001
            print(f"[reach] contacts collection failed: {exc}")
            contacts = []
        await driver._return_to_chat_list()
        cts = [c for c in contacts if c.get("peer_id")]
        print(f"[reach] Contacts found: {len(cts)}")

        pv_ids = [c["peer_id"] for c in pvs[:sample]]
        ct_ids = [c["peer_id"] for c in cts[:sample]]
        pv_res = await session.page.evaluate(RESOLVE_PEERS_JS, pv_ids) if pv_ids else []
        ct_res = await session.page.evaluate(RESOLVE_PEERS_JS, ct_ids) if ct_ids else []

        print("")
        print("[reach] ===== BRIDGE REACH: PVs vs CONTACTS =====")
        s_pv = print_reach_group("PVs (private chats)", len(pvs), pv_res)
        s_ct = print_reach_group("Contacts", len(cts), ct_res)
        print("[reach] " + "-" * 31)
        print("[reach] NOTE: resolvable peers are sent via the FAST bridge (invokeApi,")
        print("[reach]   server-ACK). Non-resolvable ones fall back to sendText/UI, which")
        print("[reach]   can still deliver (and creates the private chat). The campaign")
        print("[reach]   currently targets your CONTACTS list.")
        print("[reach] TIP: for a real end-to-end test to any single peer, use:")
        print("[reach]   python cli.py bridge-real --account " + account + " --peer <peer_id>")
        print("[reach] ==========================================")
    return 0


def cmd_direct_probe(session_path: str | None, account: str | None, url: str | None) -> int:
    """First LIVE direct-client call: help.getConfig with the reused auth_key.

    No browser. Auto-finds the newest exported session for --account, then
    tries each candidate Eitaa shard host (or --url) until one returns a
    decryptable MTProto reply. Prints exactly what each host did so we can
    pin the DC endpoint and confirm the transport is raw MTProto.
    """
    from pathlib import Path as _Path
    from direct import session as dsession, dc as dccfg, crypto
    from direct.client import DirectClient
    from direct.errors import RpcError, DirectError, SecurityError

    # Resolve the session file.
    if not session_path and account:
        matches = sorted(config.ARTIFACTS_DIR.glob(f"sessions/{account}_*.json"))
        if not matches:
            print(f"[direct] no session found: run bridge-export-session --account {account}")
            return 2
        session_path = str(matches[-1])
        print(f"[direct] using newest session: {session_path}")
    if not session_path:
        print("[direct] pass --account <name> or --session <file>")
        return 2

    sess, report = dsession.load_export(session_path)
    print(f"[direct] session: {sess.describe()}")
    if not sess.is_valid():
        print(f"[direct] session invalid; missing: {report.get('missing')}")
        return 2
    print(f"[direct] auth_key_id (home dc {sess.dc_id}): {sess.auth_key_id.hex()}")

    urls = [url] if url else dccfg.candidate_urls(sess.dc_id)
    print(f"[direct] trying {len(urls)} candidate endpoint(s)...")
    for u in urls:
        client = DirectClient(sess, url=u)
        tag = f"[direct] {u}"
        try:
            ev = client.get_config()
            if ev.get("type") == "rpc_result":
                print(f"{tag}  ✅ RPC RESULT cid=0x{ev.get('result_cid', 0):08x} "
                      f"({len(ev.get('result') or b'')} bytes)")
                print("[direct] 🎉🎉 THE HEADLESS CLIENT TALKS TO EITAA. This is the DC endpoint.")
                print(f"[direct] Lock it in: export MKWL_DC_HOSTS='{sess.dc_id}={u}'")
                return 0
            print(f"{tag}  ? no rpc_result: {ev}")
        except SecurityError as exc:
            print(f"{tag}  ✗ decrypt/auth mismatch ({exc}) — wrong DC or wrapped transport")
        except RpcError as exc:
            # Reached the server and it spoke MTProto back -> RIGHT endpoint!
            print(f"{tag}  ⚠ rpc_error: {exc}")
            print("[direct] (server answered in MTProto -> endpoint is correct; schema/params to adjust)")
            print(f"[direct] Lock it in: export MKWL_DC_HOSTS='{sess.dc_id}={u}'")
            return 0
        except DirectError as exc:
            print(f"{tag}  ✗ transport: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"{tag}  ✗ {type(exc).__name__}: {exc}")
    print("[direct] none of the candidate hosts returned MTProto.")
    print("[direct] Likely the /eitaa/ POST body is wrapped (not raw MTProto) -> we then")
    print("[direct] capture the WORKER request (CDP) to see the exact envelope.")
    return 0


def cmd_extract(run_id: str) -> int:
    out = extract_run(run_id)
    print(f"[extract] params written: {out}")
    print(summarize(out))
    return 0


def cmd_list() -> int:
    config.ensure_dirs()
    runs = sorted(p.name for p in config.ARTIFACTS_DIR.iterdir() if p.is_dir())
    if not runs:
        print("(no capture runs yet)")
    for r in runs:
        print(r)
    return 0


async def cmd_inspect(
    account: str,
    open_query: str | None,
    menu: bool,
    add_contact: bool,
    attach: bool = False,
    attach_file: str | None = None,
    login: bool = False,
) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        logged_in = await driver.is_logged_in()
        print(f"[inspect] logged_in guess: {logged_in}")
        if menu:
            info = await inspect_menu(session.page)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if add_contact:
            info = await inspect_add_contact(driver)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if attach:
            print("[inspect] opening Saved Messages (safe) and revealing the upload UI...")
            info = await inspect_attach(driver, file_path=attach_file)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if login:
            print("[inspect] dumping the auth/login page structure...")
            info = await inspect_login(driver)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if open_query:
            try:
                await driver.open_chat(open_query)
                print(f"[inspect] opened chat for query: {open_query!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"[inspect] could not open chat: {exc}")
        snapshot = await inspect_dom(session.page)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


def _normalize_ir_phone(raw: str) -> str | None:
    """Normalize an Iranian mobile number to +98XXXXXXXXXX. None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    # Iranian mobile numbers are 10 digits starting with 9.
    if len(digits) == 10 and digits.startswith("9"):
        return "+98" + digits
    return None


def _read_contacts_csv(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if not row or not row[0].strip():
                continue
            # Skip a header line if present.
            if i == 0 and row[0].strip().lower() in {"phone", "شماره", "number"}:
                continue
            phone = row[0].strip()
            first = row[1].strip() if len(row) > 1 else ""
            last = row[2].strip() if len(row) > 2 else ""
            rows.append({"phone": phone, "first": first, "last": last})
    return rows


async def cmd_add_contacts(account: str, file: str, limit: int | None) -> int:
    config.ensure_dirs()
    entries = _read_contacts_csv(file)
    if limit and limit > 0:
        entries = entries[:limit]
    if not entries:
        print("[add-contacts] no rows in", file)
        return 2

    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[add-contacts] not logged in. run: python cli.py login --account", account)
            return 2

        # Normalize first; keep invalid ones out of the browser batch.
        valid_entries = []
        results = []
        for e in entries:
            norm = _normalize_ir_phone(e["phone"])
            if norm is None:
                results.append({**e, "status": "invalid_number", "detail": "bad phone format"})
                print(f"[add-contacts] {e['phone']} -> invalid_number")
            else:
                valid_entries.append({"phone": norm, "first": e["first"], "last": e["last"]})

        batch_results = await driver.add_contacts_batch(valid_entries)
        for r in batch_results:
            results.append(r)
            print(f"[add-contacts] {r['phone']} ({r['first']} {r['last']}) -> {r['status']} {r.get('detail','')}")

        added = sum(1 for r in results if r["status"] == "added")
        not_on = sum(1 for r in results if r["status"] == "not_on_eitaa")
        invalid = sum(1 for r in results if r["status"] == "invalid_number")
        errors = sum(1 for r in results if r["status"] == "error")
        print(f"[add-contacts] summary: added={added} not_on_eitaa={not_on} "
              f"invalid={invalid} error={errors} total={len(results)}")
    return 0


def _summarize_diagnostic_run(run_dir) -> None:
    """Read the run's events.jsonl and print a self-contained diagnosis.

    The user runs on a headless server and cannot open the artifact files, so
    the key evidence (did the click reach the Add button? did a request fire?)
    is summarized straight to stdout.
    """
    import urllib.parse as _url
    from pathlib import Path as _P

    events_path = _P(run_dir) / "events.jsonl"
    if not events_path.exists():
        print("[diagnose-add-contact] no events.jsonl to analyze")
        return

    ui, http_req, console = [], [], []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        src = ev.get("source")
        if src == "ui":
            ui.append(ev)
        elif src == "http" and ev.get("kind") == "request":
            http_req.append(ev)
        elif src == "console":
            console.append(ev)

    def _is_add_button(ev: dict) -> bool:
        tgt = ev.get("target") or {}
        action = tgt.get("action") or {}
        cls = f"{tgt.get('cls', '')} {action.get('cls', '')}".lower()
        return "btn-primary" in cls or "btn-color-primary" in cls

    clicks = [e for e in ui if e.get("kind") == "click" and e.get("phase") in ("action", "trail")]
    add_clicks = [e for e in clicks if _is_add_button(e)]
    trusted_add = [e for e in add_clicks if e.get("trusted")]

    submit_http = [e for e in http_req if e.get("phase") in ("action", "trail")]
    hosts: dict[str, int] = {}
    for e in submit_http:
        try:
            host = _url.urlparse(e.get("url", "")).netloc or "?"
        except Exception:  # noqa: BLE001
            host = "?"
        hosts[host] = hosts.get(host, 0) + 1

    print("[diagnose-add-contact] --- evidence summary ---")
    print(f"  clicks (submit window): {len(clicks)} | on Add button: {len(add_clicks)} | trusted+on-Add: {len(trusted_add)}")
    if add_clicks:
        tgt = add_clicks[0].get("target", {}) or {}
        act = tgt.get("action") or tgt
        print(f"  add-button target: tag={act.get('tag')} cls={str(act.get('cls',''))[:70]} text={str(act.get('text',''))[:24]}")
    print(f"  HTTP requests after click: {len(submit_http)}")
    if hosts:
        top = ", ".join(f"{h}({n})" for h, n in sorted(hosts.items(), key=lambda x: -x[1])[:6])
        print(f"  request hosts: {top}")
    print(f"  console warnings/errors: {len(console)}")
    for e in console[:5]:
        msg = e.get("text") or e.get("error") or ""
        print(f"    - {e.get('kind')}: {str(msg)[:160]}")

    if trusted_add and not submit_http:
        print("  >> INTERPRETATION: a real click reached the Add button, but NO network request fired.")
        print("     Likely cause: the contenteditable phone value never reaches Eitaa's internal model")
        print("     (input/change events not registered) -> fix by dispatching proper input events on fill.")
    elif trusted_add and submit_http:
        print("  >> INTERPRETATION: click fired AND request(s) went out; popup staying open likely means")
        print("     the server rejected it (e.g. number not registered on Eitaa) without a visible toast.")
    elif not trusted_add:
        print("  >> INTERPRETATION: no trusted click landed on the Add button; the click did not physically reach it.")


async def cmd_diagnose_add_contact(account: str, file: str) -> int:
    """Submit exactly one contact and capture UI/network evidence safely."""
    config.ensure_dirs()
    entries = _read_contacts_csv(file)
    if not entries:
        print("[diagnose-add-contact] no rows in", file)
        return 2

    entry = entries[0]
    phone = _normalize_ir_phone(entry["phone"])
    if phone is None:
        print("[diagnose-add-contact] first row has an invalid Iranian mobile number")
        return 2

    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[diagnose-add-contact] not logged in. run: python cli.py login --account", account)
            return 2

        try:
            await driver.open_contacts_view()
        except Exception as exc:  # noqa: BLE001
            print(f"[diagnose-add-contact] could not open Contacts: {exc}")
            return 1

        rec = RunRecorder(
            session,
            "diagnose_add_contact",
            ui_diagnostics=True,
            sensitive_literals=[
                entry.get("phone", ""), phone,
                entry.get("first", ""), entry.get("last", ""),
            ],
        )
        try:
            await rec.start()
        except Exception as exc:  # noqa: BLE001
            print(f"[diagnose-add-contact] instrumentation failed before submit: {type(exc).__name__}")
            return 1
        print(f"[diagnose-add-contact] run_id={rec.run_id}")
        print("[diagnose-add-contact] submitting only the first CSV row; sensitive values are not logged")
        await rec.baseline(seconds=1)

        holder: dict[str, dict] = {}

        async def submit_one() -> None:
            holder["result"] = await driver._add_one(
                phone,
                entry.get("first", ""),
                entry.get("last", ""),
                before_submit=lambda: rec.checkpoint("pre_submit"),
                after_submit=lambda: rec.checkpoint("post_submit"),
            )

        try:
            await rec.action(submit_one)
        except Exception as exc:  # noqa: BLE001
            holder["result"] = {
                "status": "error",
                "detail": f"diagnostic action failed: {type(exc).__name__}: {exc}",
            }
        finally:
            result = holder.get(
                "result",
                {"status": "error", "detail": "submission ended before a result was returned"},
            )
            run_dir = await rec.finish(
                extra_meta={
                    "ui_diagnostics": True,
                    "result_status": result.get("status", "error"),
                }
            )

        detail = rec.scrub_sensitive(str(result.get("detail", "")))
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        artifacts = meta.get("diagnostic_artifacts", {})
        missing = [name for name, present in artifacts.items() if not present]
        print(
            f"[diagnose-add-contact] result={result.get('status')} "
            f"detail={detail}"
        )
        print(f"[diagnose-add-contact] artifacts: {run_dir}")
        if meta.get("diagnostic_complete"):
            print("[diagnose-add-contact] evidence complete: pre/post submit PNG+DOM, click events, console, HTTP/WS metadata, trace")
        else:
            print(f"[diagnose-add-contact] evidence incomplete; missing={missing} ui_events={meta.get('event_counts', {}).get('ui', 0)}")
        _summarize_diagnostic_run(run_dir)
        return 0 if result.get("status") == "added" and meta.get("diagnostic_complete") else 1


async def cmd_send_file(
    account: str,
    to: str | None,
    saved: bool,
    file: str | None,
    caption: str,
    as_photo: bool = False,
) -> int:
    config.ensure_dirs()
    if not saved and not to:
        print("[send-file] specify --to <chat> or --saved")
        return 2

    # If no file is given, auto-create a small test file so the user can test
    # with zero setup.
    created_temp = False
    if not file:
        import tempfile
        fd, file = tempfile.mkstemp(suffix=".txt", prefix="mkwl_test_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("Mkwlsoso test file\n")
        created_temp = True
        if not caption:
            caption = "تست فایل از Mkwlsoso"
        print(f"[send-file] no --file given; created a test file: {file}")

    if not os.path.isfile(file):
        print(f"[send-file] file not found: {file}")
        return 2

    try:
        async with open_session(account) as session:
            driver = EitaaDriver(session)
            await driver.open()
            if not await driver.is_logged_in():
                print("[send-file] not logged in. run: python cli.py login --account", account)
                return 2
            target = "Saved Messages" if saved else to
            print(f"[send-file] sending {file!r} to {target!r}"
                  + (f" with caption {caption!r}" if caption else ""))
            res = await driver.send_file(
                file, caption=caption, query=to, to_saved=saved, as_photo=as_photo
            )
            print(f"[send-file] ok={res.ok} to={res.to!r} detail={res.detail}")
            return 0 if res.ok else 1
    finally:
        if created_temp:
            try:
                os.remove(file)
            except OSError:
                pass


async def cmd_stats(account: str) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[stats] not logged in. run: python cli.py login --account", account)
            return 2
        print("[stats] counting (this scrolls chats + contacts, takes a bit)...")
        stats = await driver.get_stats()
        c = stats["contacts"]
        c_str = "unknown (contacts view failed)" if c < 0 else str(c)
        print(f"[stats] account={account}")
        print(f"        contacts (مخاطبین): {c_str}")
        print(f"        private chats (پی‌وی): {stats['pvs']}")
    return 0


async def cmd_contacts(account: str, out: str) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[contacts] not logged in. run: python cli.py login --account", account)
            return 2
        print("[contacts] opening Contacts view and scrolling the full list...")
        try:
            contacts = await driver.collect_all_contacts()
        except Exception as exc:  # noqa: BLE001
            print(f"[contacts] failed to open contacts view: {exc}")
            print("[contacts] run this to reveal the menu, then send me the output:")
            print(f"           DISPLAY=:99 python cli.py inspect --account {account} --menu")
            return 1
        out_path = Path(out)
        lines = [c["title"] for c in contacts if c.get("title")]
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[contacts] collected {len(lines)} contacts -> {out_path}")
        # Also save structured data (title + peer_id) alongside.
        json_path = out_path.with_suffix(".json")
        json_path.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[contacts] structured data -> {json_path}")
    return 0


async def cmd_chats(account: str) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        titles = await driver.list_chat_titles(limit=20)
        print(f"[chats] {len(titles)} visible chats (use one of these names with --to):")
        for i, t in enumerate(titles, 1):
            print(f"  {i:2d}. {t}")
    return 0


async def cmd_send(account: str, to: str, text: str, no_verify: bool) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[send] not logged in. run: python cli.py login --account", account)
            return 2
        print(f"[send] sending to {to!r} ...")
        result = await driver.send_text(to, text, verify=not no_verify)
        print(f"[send] ok={result.ok} detail={result.detail}")
        return 0 if result.ok else 1


async def cmd_collect(account: str, out: str, users_only: bool) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[collect] not logged in. run: python cli.py login --account", account)
            return 2
        print("[collect] scrolling chat list (this can take a bit)...")
        chats = await driver.collect_all_chats()
        if users_only:
            chats = [c for c in chats if c.get("kind") == "user"]
        lines = [c["title"] for c in chats if c.get("title")]
        out_path = Path(out)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[collect] wrote {len(lines)} names to {out_path}")
        print("[collect] EDIT this file to keep only the recipients you want, then run campaign.")
    return 0


def _read_recipients(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


async def cmd_campaign(account: str, file: str | None, text: str | None, resume: str | None, limit: int | None) -> int:
    config.ensure_dirs()
    if resume:
        job = JobState.load(resume)
        print(f"[campaign] resuming {job.job_id} (status was {job.status})")
    else:
        if not file or text is None:
            print("[campaign] need --file and --text to start a new campaign")
            return 2
        names = _read_recipients(file)
        if not names:
            print("[campaign] recipient file is empty")
            return 2
        total = len(names)
        job = create_campaign(account, text, names, limit=limit)
        if limit and limit > 0:
            print(f"[campaign] TEST MODE: using first {len(job.recipients)} of {total} recipients")
        print(f"[campaign] created job {job.job_id} with {len(job.recipients)} recipients")
    await run_campaign(job)
    return 0


def cmd_campaign_status(job_id: str) -> int:
    job = JobState.load(job_id)
    c = job.counts()
    print(f"job {job.job_id} status={job.status}")
    print(f"  total={c['total']} sent={c['sent']} failed={c['failed']} "
          f"pending={c['pending']} skipped={c['skipped']}")
    failed = [r.name for r in job.recipients if r.status == 'failed']
    if failed:
        print(f"  failed ({len(failed)}): {', '.join(failed[:15])}")
    return 0


def cmd_campaign_stop(job_id: str) -> int:
    request_stop(job_id)
    print(f"[campaign] stop flag written for {job_id}. it will stop after the current recipient.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Eitaa web capture tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="open browser to log in manually")
    p_login.add_argument("--account", required=True)

    p_cap = sub.add_parser("capture", help="record one operation")
    p_cap.add_argument("--account", required=True)
    p_cap.add_argument("--op", required=True, help="label, e.g. send_text, send_contact, list_contacts")
    p_cap.add_argument(
        "--auto",
        action="store_true",
        help="do not wait for a manual action (baseline+trail only)",
    )

    p_an = sub.add_parser("analyze", help="build report.md for a run")
    p_an.add_argument("--run", required=True)

    p_probe = sub.add_parser("probe", help="deep protocol capture (hooks + assets + dossier)")
    p_probe.add_argument("--account", required=True)
    p_probe.add_argument("--op", required=True, help="label, e.g. login, contacts, send_text, send_file")
    p_probe.add_argument("--auto", action="store_true", help="no manual action (baseline+trail only)")

    p_bridge = sub.add_parser(
        "bridge",
        help="discover if a high-level send bridge exists at the JS worker boundary",
    )
    p_bridge.add_argument("--account", required=True)
    p_bridge.add_argument(
        "--manual", action="store_true",
        help="you send the marker to Saved Messages yourself instead of automating it",
    )

    p_bsend = sub.add_parser(
        "bridge-send",
        help="verify the bridge can actually send (to your Saved Messages only)",
    )
    p_bsend.add_argument("--account", required=True)

    p_breal = sub.add_parser(
        "bridge-real",
        help="send ONE real message via the bridge (defaults to your Saved Messages)",
    )
    p_breal.add_argument("--account", required=True)
    p_breal.add_argument("--peer", default=None, help="peer_id target (default: self/Saved Messages)")
    p_breal.add_argument("--text", default=None, help="message text (default: an auto test string)")

    p_bfile = sub.add_parser(
        "bridge-file",
        help="investigate fast file send (upload once + forward/reuse) to your Saved Messages",
    )
    p_bfile.add_argument("--account", required=True)
    p_bfile.add_argument("--file", default=None, help="file to test (default: an auto tiny .txt)")

    p_reach = sub.add_parser(
        "bridge-reach",
        help="measure how far the bridge reaches (PVs vs Contacts); sends nothing",
    )
    p_reach.add_argument("--account", required=True)
    p_reach.add_argument("--sample", type=int, default=40,
                         help="how many peers per group to check (default 40)")

    p_export = sub.add_parser(
        "bridge-export-session",
        help="export the profile's MTProto session (auth_key/salt/dc/user) for the direct client",
    )
    p_export.add_argument("--account", required=True)

    p_txcap = sub.add_parser(
        "direct-capture-transport",
        help="pin Eitaa's real MTProto URL + transport envelope (main-thread; sees media only)",
    )
    p_txcap.add_argument("--account", required=True)

    p_wcap = sub.add_parser(
        "direct-capture-worker",
        help="capture the EXACT bytes the MTProto Worker sends (fetch/XHR/WebSocket)",
    )
    p_wcap.add_argument("--account", required=True)

    p_capall = sub.add_parser(
        "direct-capture-all",
        help="capture wire bytes for ALL ops in one run: send text + send file + add contact",
    )
    p_capall.add_argument("--account", required=True)
    p_capall.add_argument("--phone", default="+989000000000",
                        help="throwaway test number for the add-contact capture (default: fake/unassigned)")
    p_capall.add_argument("--first", default="MkwlTest", help="first name for the test contact")

    p_dprobe = sub.add_parser(
        "direct-probe",
        help="first LIVE direct-client call (help.getConfig) using an exported session",
    )
    p_dprobe.add_argument("--account", default=None, help="use the newest exported session for this account")
    p_dprobe.add_argument("--session", default=None, help="explicit path to a session JSON")
    p_dprobe.add_argument("--url", default=None, help="force a single DC URL (skip auto-try)")

    p_drep = sub.add_parser(
        "direct-replay",
        help="PROVE the direct transport: resend ONE captured idempotent request (config) from pure Python",
    )
    p_drep.add_argument("--account", required=True, help="account whose newest worker-capture to replay")
    p_drep.add_argument("--index", type=int, default=None,
                        help="replay a specific record index (default: auto-pick the smallest API request)")

    p_dsend = sub.add_parser(
        "direct-send",
        help="send a TEXT to Saved Messages with NO browser (direct MTProto, uses newest capture's session)",
    )
    p_dsend.add_argument("--account", required=True)
    p_dsend.add_argument("--text", required=True, help="message text to send to your own Saved Messages")
    p_dsend.add_argument("--url", default=None, help="override the shard URL (default: majid.eitaa.com)")

    p_dimp = sub.add_parser(
        "direct-import",
        help="import ONE contact with NO browser (direct MTProto)",
    )
    p_dimp.add_argument("--account", required=True)
    p_dimp.add_argument("--phone", required=True, help="phone number to import (use a test number)")
    p_dimp.add_argument("--first", default="Test", help="first name")
    p_dimp.add_argument("--last", default="", help="last name")
    p_dimp.add_argument("--url", default=None, help="override the shard URL")

    p_fsend = sub.add_parser(
        "bridge-file-send",
        help="end-to-end test the production file path (upload once + reuse) to Saved Messages",
    )
    p_fsend.add_argument("--account", required=True)
    p_fsend.add_argument("--peer", default=None, help="target peer_id (default: self/Saved Messages)")
    p_fsend.add_argument("--file", default=None, help="file to send (default: an auto tiny .txt)")

    p_blogin = sub.add_parser(
        "bridge-login",
        help="log in via the bridge (phone + code here, no noVNC); one code request, flood-safe",
    )
    p_blogin.add_argument("--account", required=True, help="profile name for this account")
    p_blogin.add_argument("--phone", required=True, help="phone number (e.g. 0930... or 98930...)")

    p_ext = sub.add_parser("extract", help="mine MTProto params (DCs/api/layer/RSA) from a run's assets")
    p_ext.add_argument("--run", required=True)

    sub.add_parser("list", help="list capture runs")

    p_insp = sub.add_parser("inspect", help="print structural DOM snapshot")
    p_insp.add_argument("--account", required=True)
    p_insp.add_argument("--open", default=None, help="optional chat name to open before inspecting")
    p_insp.add_argument("--menu", action="store_true", help="open the sidebar menu and dump its items")
    p_insp.add_argument("--add-contact", dest="add_contact", action="store_true",
                        help="open the add-contact popup and dump its form")
    p_insp.add_argument("--attach", action="store_true",
                        help="open Saved Messages and reveal the file-upload UI (safe)")
    p_insp.add_argument("--attach-file", dest="attach_file", default=None,
                        help="optional file to attach so the caption box + send button are revealed (not sent)")
    p_insp.add_argument("--login", action="store_true",
                        help="dump the auth/login page structure (run on a FRESH, not-logged-in profile)")

    p_chats = sub.add_parser("chats", help="list visible chat titles (choose one for --to)")
    p_chats.add_argument("--account", required=True)

    p_contacts = sub.add_parser("contacts", help="collect ALL contacts from the Contacts view")
    p_contacts.add_argument("--account", required=True)
    p_contacts.add_argument("--out", default="contacts.txt", help="output file path")

    p_stats = sub.add_parser("stats", help="show account stats (contacts count + PV count)")
    p_stats.add_argument("--account", required=True)

    p_addc = sub.add_parser("add-contacts", help="create contacts from a CSV (phone,first_name,last_name)")
    p_addc.add_argument("--account", required=True)
    p_addc.add_argument("--file", required=True, help="CSV file: phone,first_name,last_name")
    p_addc.add_argument("--limit", type=int, default=None, help="only process the first N rows (safe test)")

    p_diag_add = sub.add_parser(
        "diagnose-add-contact",
        help="submit the first CSV row and capture exact UI/network diagnostics",
    )
    p_diag_add.add_argument("--account", required=True)
    p_diag_add.add_argument("--file", required=True, help="CSV file; only the first row is used")

    p_send = sub.add_parser("send", help="send one text message via the browser driver")
    p_send.add_argument("--account", required=True)
    p_send.add_argument("--to", required=True, help="chat/contact name or username to open")
    p_send.add_argument("--text", required=True, help="message text to send")
    p_send.add_argument("--no-verify", action="store_true", help="skip DOM verification")

    p_sfile = sub.add_parser("send-file", help="upload and send a file (optionally with a caption)")
    p_sfile.add_argument("--account", required=True)
    p_sfile.add_argument("--to", default=None, help="chat/contact name to send to")
    p_sfile.add_argument("--saved", action="store_true", help="send to your own Saved Messages (safe test)")
    p_sfile.add_argument("--file", default=None, help="file to send; if omitted a small test .txt is created")
    p_sfile.add_argument("--caption", default="", help="optional caption text")
    p_sfile.add_argument("--as-photo", dest="as_photo", action="store_true",
                        help="send as photo/video (عکس یا ویدیو) instead of a document (فایل)")

    p_col = sub.add_parser("collect", help="scroll all chats and write names to a file")
    p_col.add_argument("--account", required=True)
    p_col.add_argument("--out", default="recipients_all.txt", help="output file path")
    p_col.add_argument("--users-only", action="store_true", help="exclude groups/channels")

    p_camp = sub.add_parser("campaign", help="broadcast a text to a recipient list")
    p_camp.add_argument("--account", required=True)
    p_camp.add_argument("--file", default=None, help="recipients file (one name per line)")
    p_camp.add_argument("--text", default=None, help="message text to broadcast")
    p_camp.add_argument("--resume", default=None, help="resume an existing job_id")
    p_camp.add_argument("--limit", type=int, default=None, help="only send to the first N recipients (safe test)")

    p_cs = sub.add_parser("campaign-status", help="show a campaign's progress")
    p_cs.add_argument("--job", required=True)

    p_cx = sub.add_parser("campaign-stop", help="request a campaign to stop cleanly")
    p_cx.add_argument("--job", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "login":
        return asyncio.run(cmd_login(args.account))
    if args.command == "capture":
        return asyncio.run(cmd_capture(args.account, args.op, manual=not args.auto))
    if args.command == "analyze":
        return cmd_analyze(args.run)
    if args.command == "probe":
        return asyncio.run(cmd_probe(args.account, args.op, manual=not args.auto))
    if args.command == "bridge":
        return asyncio.run(cmd_bridge(args.account, manual=args.manual))
    if args.command == "bridge-send":
        return asyncio.run(cmd_bridge_send(args.account))
    if args.command == "bridge-real":
        return asyncio.run(cmd_bridge_real(args.account, args.peer, args.text))
    if args.command == "bridge-file":
        return asyncio.run(cmd_bridge_file(args.account, args.file))
    if args.command == "bridge-reach":
        return asyncio.run(cmd_bridge_reach(args.account, args.sample))
    if args.command == "bridge-export-session":
        return asyncio.run(cmd_bridge_export_session(args.account))
    if args.command == "direct-capture-transport":
        return asyncio.run(cmd_direct_capture_transport(args.account))
    if args.command == "direct-capture-worker":
        return asyncio.run(cmd_direct_capture_worker(args.account))
    if args.command == "direct-capture-all":
        return asyncio.run(cmd_direct_capture_all(args.account, args.phone, args.first))
    if args.command == "direct-probe":
        return cmd_direct_probe(args.session, args.account, args.url)
    if args.command == "direct-replay":
        return cmd_direct_replay(args.account, args.index)
    if args.command == "direct-send":
        return cmd_direct_send(args.account, args.text, args.url)
    if args.command == "direct-import":
        return cmd_direct_import(args.account, args.phone, args.first, args.last, args.url)
    if args.command == "bridge-file-send":
        return asyncio.run(cmd_bridge_file_send(args.account, args.peer, args.file))
    if args.command == "bridge-login":
        return asyncio.run(cmd_bridge_login(args.account, args.phone))
    if args.command == "extract":
        return cmd_extract(args.run)
    if args.command == "list":
        return cmd_list()
    if args.command == "inspect":
        return asyncio.run(cmd_inspect(
            args.account, args.open, args.menu, args.add_contact,
            args.attach, args.attach_file, args.login,
        ))
    if args.command == "chats":
        return asyncio.run(cmd_chats(args.account))
    if args.command == "contacts":
        return asyncio.run(cmd_contacts(args.account, args.out))
    if args.command == "stats":
        return asyncio.run(cmd_stats(args.account))
    if args.command == "add-contacts":
        return asyncio.run(cmd_add_contacts(args.account, args.file, args.limit))
    if args.command == "diagnose-add-contact":
        return asyncio.run(cmd_diagnose_add_contact(args.account, args.file))
    if args.command == "send":
        return asyncio.run(cmd_send(args.account, args.to, args.text, args.no_verify))
    if args.command == "send-file":
        return asyncio.run(cmd_send_file(
            args.account, args.to, args.saved, args.file, args.caption, args.as_photo
        ))
    if args.command == "collect":
        return asyncio.run(cmd_collect(args.account, args.out, args.users_only))
    if args.command == "campaign":
        return asyncio.run(cmd_campaign(args.account, args.file, args.text, args.resume, args.limit))
    if args.command == "campaign-status":
        return cmd_campaign_status(args.job)
    if args.command == "campaign-stop":
        return cmd_campaign_stop(args.job)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
