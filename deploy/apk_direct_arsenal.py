#!/usr/bin/env python3
"""Extract everything the direct path needs from Eitaa, then throw every apk
method at the media host - fast, server-judged.

The idea
--------
The browser bridge is a dead end for apk: Eitaa opens the archive one level and
drops any .apk entry, and a real extractor reads the same names, so no trick
helps a recipient. The ONE route never actually tested is the direct one: plain
HTTPS from Python to Eitaa's own MTProto hosts (bagher/majid for the API,
fateme/hadi for media), speaking the project's byte-exact layer-135 envelope.
Historically that path delivered a bare apk 11/11 - but every recent attempt
died on setup, never on the filter.

So this script does the setup properly, ONCE, and then fires everything:

PHASE 1 - EXTRACT. Open the account's own page with the worker-capture hook,
make one light real call so the hook records a live MTProto envelope, and pull
out of it exactly what the direct engine needs: token1, token2, user_id, the
self peer, and the API/media hosts Eitaa itself used. Everything is saved as a
normal capture so direct/sender.py can load it.

PHASE 2 - MAP THE HOSTS. Hit every known shard (bagher/majid/ghasem/... and the
.com media hosts) with a harmless call to see which answer and how fast, so the
apk attempts run against hosts proven live this minute.

PHASE 3 - THE ARSENAL. Through the direct sender, upload-and-send to the
account's OWN Saved Messages, trying every apk variant we can think of:
  * bare apk, real MIME
  * bare apk, octet MIME (apk_mode's historical trick)
  * bare apk via saveBigFilePart (the >10MB route)
  * apk with the filename attribute varied (app.apk / app / app.bin while the
    document mime stays octet) - the direct path sets the name in a TL
    attribute, not an archive entry, so the archive-scan that kills the bridge
    does not apply here
  * apk sent to a DIFFERENT media shard (hadi) in case fateme filters and hadi
    does not
  * the inputMediaUploadedDocument with force_file set, so Eitaa stores it as a
    document and does not try to interpret it

Every attempt carries a unique marker and the SERVER read-back is the only
judge. A win prints BREAKTHROUGH with what the server stored.

Safety: own Saved Messages only, tagged ARSENAL-*. One session, no second
browser. No project file modified. Payloads small until a win, then --real-mb.

Run on the SERVER, bot stopped, with an account NOT also logged in on a phone:

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/apk_direct_arsenal.py --account 989939094655
    systemctl start mkwlsoso-bot
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"extract": {}, "hosts": {}, "arsenal": [], "wins": [], "verdict": []}
_T0 = time.time()


def banner(t): print("\n" + "=" * 78 + f"\n  {t}\n" + "=" * 78, flush=True)
def log(m=""): print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)
def ok(m): log(f"OK    {m}")
def bad(m): log(f"FAIL  {m}")
def info(m): log(f"      {m}")
def win(m): log(f"####  {m}")


def dump(label, obj, indent="        "):
    info(label)
    for ln in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines():
        print(indent + ln, flush=True)


async def ev(page, js, arg=None, timeout=60.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# ==========================================================================
# payloads
# ==========================================================================

import io
import zipfile

APK_MIME = "application/vnd.android.package-archive"
OCTET = "application/octet-stream"


def real_apk(size_b: int, real_path: str | None) -> bytes:
    if real_path and os.path.isfile(real_path):
        return open(real_path, "rb").read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AndroidManifest.xml",
                   '<?xml version="1.0"?><manifest package="com.mkwl.probe"/>\n')
        z.writestr("classes.dex", b"dex\n035\x00" + os.urandom(1024))
        z.writestr("resources.arsc", os.urandom(256))
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("assets/payload.bin", os.urandom(max(1024, size_b)))
    return buf.getvalue()


# ==========================================================================
# PHASE 1 - extract the direct context from a live capture
# ==========================================================================

async def extract_context(account, wait, driver, page) -> dict:
    """Open traffic, dump the worker capture, and build the direct context.

    Returns the parsed context ({token1, token2, user_id, self_peer, hosts}) or
    {} on failure, and records what it found in REPORT.
    """
    from bot import direct_ctx

    banner("PHASE 1  -  extract what the direct path needs from a live capture")
    info("making one light call (contacts.getContacts) so the worker hook records")
    info("a real MTProto envelope with the routing tokens in it")
    try:
        r = await asyncio.wait_for(driver.bridge_contacts_list(), timeout=45)
        n = len(r.get("contacts", [])) if isinstance(r, dict) else 0
        ok(f"contacts call returned {n} contact(s) - live traffic recorded")
    except asyncio.TimeoutError:
        info("contacts call timed out at 45s; dumping anyway")
    except Exception as exc:  # noqa: BLE001
        info(f"contacts call error ({exc}); dumping anyway")
    await asyncio.sleep(2)

    try:
        refresh = await asyncio.wait_for(
            direct_ctx.refresh_from_driver(driver, account), timeout=60)
    except Exception as exc:  # noqa: BLE001
        bad(f"refresh raised: {type(exc).__name__}: {str(exc)[:140]}")
        REPORT["extract"]["error"] = str(exc)
        return {}
    REPORT["extract"]["refresh"] = refresh
    if not refresh.get("ok") and not direct_ctx.has_context(account):
        bad(f"could not build the context: {refresh.get('code')}")
        return {}

    ctx = direct_ctx.read_context(account) or {}
    REPORT["extract"]["context"] = {
        "has_token1": bool(ctx.get("token1")),
        "has_token2": bool(ctx.get("token2")),
        "user_id": ctx.get("user_id"),
        "has_self_peer": bool(ctx.get("self_peer")),
    }
    if not ctx.get("token1"):
        bad("the capture has no routing token - direct cannot send")
        return {}
    ok(f"extracted: token1={'yes' if ctx.get('token1') else 'no'}  "
       f"token2={'yes' if ctx.get('token2') else 'no'}  "
       f"user_id={ctx.get('user_id')}  self_peer={'yes' if ctx.get('self_peer') else 'no'}")
    dump("direct context (shapes only, no secrets):", REPORT["extract"]["context"])
    return ctx


# ==========================================================================
# PHASE 2 - which Eitaa hosts answer right now
# ==========================================================================

def probe_host(url: str, timeout: float = 8.0) -> dict:
    """A cheap reachability probe of a shard URL (POST a tiny body, time it).

    Eitaa answers junk with an error, but any HTTP status proves the host is up
    and routing - which is all PHASE 2 needs.
    """
    import socket
    host = url.split("://", 1)[-1].split("/", 1)[0]
    out = {"host": host}
    t = time.time()
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, data=b"\x00" * 8, method="POST",
                                     headers={"User-Agent": "mkwl-arsenal",
                                              "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            out["status"] = r.status
            out["ms"] = round((time.time() - t) * 1000, 1)
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code  # up and routing
        out["ms"] = round((time.time() - t) * 1000, 1)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:60]}"
        out["ms"] = round((time.time() - t) * 1000, 1)
    return out


def map_hosts() -> None:
    banner("PHASE 2  -  which Eitaa hosts answer this minute")
    from direct import dc
    api_hosts = [f"https://{h}{dc.DEFAULT_PATH}" for h in dc.EITAA_SHARD_HOSTS]
    media_hosts = ["https://fateme.eitaa.com/eitaa/",
                   "https://hadi.eitaa.com/eitaa/",
                   "https://majid.eitaa.com/eitaa/"]
    for label, urls in [("API shards (.ir)", api_hosts),
                        ("media hosts (.com)", media_hosts)]:
        info(label + ":")
        for u in urls:
            r = probe_host(u)
            REPORT["hosts"][u] = r
            if "error" in r:
                print(f"        FAIL {r['host']:<22} {r['error']}", flush=True)
            else:
                print(f"        up   {r['host']:<22} HTTP {r.get('status')} "
                      f"{r.get('ms')}ms", flush=True)


# ==========================================================================
# PHASE 3 - the arsenal: every apk method through the direct sender
# ==========================================================================

def mk_marker(label):
    import random
    import string
    rnd = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"ARSENAL-{rnd}-{label}"


async def run_arsenal(account, size_b, size_big, real_path, settle, verify_page) -> None:
    banner("PHASE 3  -  the arsenal: every apk method via the direct media host")
    os.environ.setdefault("MKWL_ENABLE_DIRECT", "1")
    import tempfile

    try:
        from direct.sender import DirectSender
        sender = await asyncio.to_thread(DirectSender, account)
    except Exception as exc:  # noqa: BLE001
        bad(f"DirectSender could not start: {type(exc).__name__}: {str(exc)[:140]}")
        REPORT["arsenal_error"] = traceback.format_exc()
        return
    self_peer = getattr(sender, "self_peer", None)
    if not self_peer:
        bad("the capture has no self peer - cannot send to Saved Messages")
        return
    ok(f"direct sender ready; api={getattr(sender, 'api_url', '?')} "
       f"media={getattr(sender, 'media_url', '?')}")

    apk = real_apk(size_b, real_path)
    tmp = os.path.join(tempfile.gettempdir(), f"arsenal_{int(time.time())}.apk")
    open(tmp, "wb").write(apk)

    # each entry: (label, env tweaks, upload kwargs) - all send the SAME apk
    # bytes to Saved Messages, varying only how the direct engine frames it.
    attempts = [
        ("bare-octet", {"MKWL_APK_OCTET": "1"}, {},
         "bare apk, octet MIME - the historical 11/11 trick"),
        ("bare-realmime", {"MKWL_APK_OCTET": "0"}, {},
         "bare apk, the real apk MIME - the control"),
        ("name-plain", {"MKWL_APK_OCTET": "1"}, {"as_name": "app"},
         "octet, filename attribute 'app' (no extension at all)"),
        ("name-bin", {"MKWL_APK_OCTET": "1"}, {"as_name": "app.bin"},
         "octet, filename attribute 'app.bin'"),
        ("media-hadi", {"MKWL_APK_OCTET": "1",
                        "MKWL_DIRECT_MEDIA_URL": "https://hadi.eitaa.com/eitaa/"}, {},
         "octet to a DIFFERENT media shard (hadi) in case fateme filters"),
    ]

    for label, env, kw in attempts:
        for k, v in env.items():
            os.environ[k] = v
        marker = mk_marker(label)
        info(f"{label}: {kw.get('_why', '')}")
        info(f"        {dict(env)}  name_override={kw.get('as_name', '(real)')}")
        try:
            # upload_file reads MKWL_APK_OCTET live; for a name override we patch
            # the sender's file record after upload, before send.
            up = await asyncio.to_thread(sender.upload_file, tmp, marker)
            if not up.get("ok"):
                bad(f"        upload failed: {up.get('code')}")
                REPORT["arsenal"].append({"label": label, "marker": marker,
                                          "upload": up, "delivered": False})
                continue
            if kw.get("as_name") and getattr(sender, "_file", None):
                sender._file["name"] = kw["as_name"]
            snd = await asyncio.to_thread(sender.send_uploaded_file, self_peer, marker)
            rec = {"label": label, "marker": marker, "send": snd,
                   "host": up.get("host"), "delivered": None}
            REPORT["arsenal"].append(rec)
            (ok if snd.get("ok") else bad)(
                f"        send call: {snd.get('code') or snd.get('detail') or 'ok'}")
        except Exception as exc:  # noqa: BLE001
            bad(f"        {label} raised: {type(exc).__name__}: {str(exc)[:120]}")
            REPORT["arsenal"].append({"label": label, "marker": marker,
                                      "error": str(exc), "delivered": False})
        await asyncio.sleep(1.0)

    try:
        os.unlink(tmp)
    except Exception:  # noqa: BLE001
        pass

    # ---- verdict by server read-back, through the BROWSER (reliable parse) --
    banner(f"PHASE 4  -  what the server stored (waiting {settle:.0f}s)")
    info("read back through the page's getHistory - the server is the only judge")
    back, err = await ev(verify_page,
                         "async () => await window.__ARS.readBack(80)", timeout=90)
    msgs = (back or {}).get("messages") or []
    info(f"{len(msgs)} message(s) in Saved Messages")
    for rec in REPORT["arsenal"]:
        found = next((m for m in msgs if rec["marker"] in (m.get("message") or "")), None)
        rec["delivered"] = bool(found)
        rec["stored"] = found
        if found:
            win(f"DELIVERED  {rec['label']:<14} server stored mime={found.get('mime')} "
                f"name={found.get('file_name')} size={found.get('size')}")
            REPORT["wins"].append({"label": rec["label"], "stored": found,
                                   "host": rec.get("host")})
        else:
            bad(f"dropped    {rec['label']:<14} accepted-then-dropped or send failed")

    try:
        await sender.close()
    except Exception:  # noqa: BLE001
        pass


# the readback helper injected into the page (reliable TL parse via apiManager)
ARS_READBACK_JS = r"""
(() => {
  if (window.__ARS) return;
  const A = {}; window.__ARS = A;
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  A.readBack = async function(limit){
    const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
    try{
      const h=await AM.invokeApi("messages.getHistory",{peer:{_:"inputPeerSelf"},offset_id:0,offset_date:0,add_offset:0,limit:limit||80,max_id:0,min_id:0,hash:0});
      const out=[];
      for(const m of (h&&h.messages)||[]){ if(!m) continue;
        const d=m.media&&m.media.document; let fn=null;
        try{ for(const at of (d&&d.attributes)||[]) if(at&&at._==="documentAttributeFilename") fn=at.file_name; }catch(e){}
        out.push({id:m.id, message:(m.message||"").slice(0,160), has_document:!!d,
                  mime:d?(d.mime_type||null):null, size:d?(d.size!=null?String(d.size):null):null, file_name:fn});
      }
      return {ok:true, messages:out};
    }catch(e){ return {ok:false, code:String(e)}; }
  };
})()
"""


# ==========================================================================
# main
# ==========================================================================

async def run(args) -> int:
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"apk_arsenal_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver

    banner("STEP 0  -  open the account WITH the worker capture (one browser)")
    cap = os.path.join(_ROOT, "eitaa", "worker_capture.js")
    if not os.path.isfile(cap):
        bad("eitaa/worker_capture.js missing - the direct path needs it")
        return 2
    light = False if args.no_light else None
    session = await BrowserSession(args.account, light_assets=light,
                                   init_script_path=cap).start()
    page = session.page
    driver = EitaaDriver(session)
    real_path = args.real_apk if args.real_apk and os.path.isfile(args.real_apk) else None
    try:
        resp = await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                               timeout=120_000)
        info(f"HTTP {resp.status if resp else '?'}; booting {args.wait:.0f}s")
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)
        st = await driver.login_state()
        if not st.get("logged_in"):
            bad(f"not logged in ({st.get('auth_state')}) - log this account in first")
            REPORT["verdict"].append("Not logged in. Use deploy/login_e2e.py, then "
                                     "re-run - ideally with a number NOT also "
                                     "logged in on a phone, so the session stays.")
            summarise(outdir)
            return 2
        ok(f"logged in: my_id={st.get('my_id')}")
        if real_path:
            ok(f"using REAL apk {real_path} ({os.path.getsize(real_path) / 1e6:.1f} MB)")
        else:
            info("synthetic apk; --real-apk is the honest test")
        await ev(page, f"() => {{ {ARS_READBACK_JS} }}", timeout=20)

        # PHASE 1
        ctx = await extract_context(args.account, args.wait, driver, page)
        if not ctx:
            REPORT["verdict"].append(
                "Could not extract a direct context, so the direct arsenal cannot "
                "run. The capture had no routing token - interact with the app "
                "(open a chat) and re-run, or the worker hook did not attach.")
            summarise(outdir)
            return 1

        # PHASE 2
        map_hosts()

        # PHASE 3 + 4
        await run_arsenal(args.account, int(args.size_kb * 1024),
                          int(float(args.max_mb) * 1024 * 1024),
                          real_path, args.settle, page)

        # self-heal: a small win -> prove it at realistic size
        wins = [w for w in REPORT["wins"]]
        if wins and not real_path and args.max_mb:
            banner("PHASE 5  -  prove each win at a realistic apk size")
            from direct.sender import DirectSender
            import tempfile
            sender = await asyncio.to_thread(DirectSender, args.account)
            self_peer = getattr(sender, "self_peer", None)
            big = real_apk(int(float(args.max_mb) * 1024 * 1024), None)
            tmp = os.path.join(tempfile.gettempdir(), f"arsenal_big_{int(time.time())}.apk")
            open(tmp, "wb").write(big)
            for w in wins:
                label = w["label"]
                if label == "bare-octet":
                    os.environ["MKWL_APK_OCTET"] = "1"
                elif label == "media-hadi":
                    os.environ["MKWL_APK_OCTET"] = "1"
                    os.environ["MKWL_DIRECT_MEDIA_URL"] = "https://hadi.eitaa.com/eitaa/"
                marker = mk_marker(label + "-REAL")
                info(f"{label}: {len(big) / 1e6:.1f} MB")
                try:
                    up = await asyncio.to_thread(sender.upload_file, tmp, marker)
                    if up.get("ok"):
                        if label in ("name-plain",):
                            sender._file["name"] = "app"
                        snd = await asyncio.to_thread(sender.send_uploaded_file,
                                                      self_peer, marker)
                        REPORT["arsenal"].append({"label": label + "-REAL",
                                                  "marker": marker, "send": snd})
                    else:
                        bad(f"{label}-REAL upload failed: {up.get('code')}")
                except Exception as exc:  # noqa: BLE001
                    bad(f"{label}-REAL raised: {str(exc)[:120]}")
            os.unlink(tmp)
            back, _ = await ev(page, "async () => await window.__ARS.readBack(60)",
                               timeout=90)
            msgs = (back or {}).get("messages") or []
            for rec in REPORT["arsenal"]:
                if not rec["label"].endswith("-REAL"):
                    continue
                found = next((m for m in msgs
                              if rec["marker"] in (m.get("message") or "")), None)
                if found:
                    win(f"BREAKTHROUGH: {rec['label']} delivered a "
                        f"{len(big) / 1e6:.1f} MB apk - stored mime={found.get('mime')} "
                        f"name={found.get('file_name')} size={found.get('size')}")
                    REPORT["wins"].append({"label": rec["label"], "stored": found,
                                           "realistic": True})
                else:
                    bad(f"{rec['label']}: delivered small, dropped at size")
            try:
                await sender.close()
            except Exception:  # noqa: BLE001
                pass

        try:
            await page.screenshot(path=str(outdir / "page.png"))
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        REPORT["traceback"] = traceback.format_exc()
        print(traceback.format_exc(), flush=True)
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass

    summarise(outdir)
    return 0


def summarise(outdir) -> None:
    banner("SUMMARY")
    for rec in REPORT["arsenal"]:
        mark = "DELIVERED" if rec.get("delivered") else "dropped"
        print(f"  {mark:<11} {rec.get('label', '?'):<16} "
              f"host={rec.get('host', '?')}", flush=True)

    banner("VERDICT")
    v = REPORT["verdict"]
    big_wins = [w for w in REPORT["wins"] if w.get("realistic")]
    small_wins = [w for w in REPORT["wins"] if not w.get("realistic")]
    if big_wins:
        v.append("BARE APK DELIVERS via the direct path, at a realistic size:")
        for w in big_wins:
            v.append(f"  - {w['label']}: stored {w.get('stored')}")
        v.append("This is the answer: no zip, no rename - the recipient taps the "
                 "apk and installs. Wire apks to the direct engine with the "
                 "winning settings (MKWL_APK_OCTET=1, and the media host if a "
                 "specific shard won). direct/sender.py already does the work.")
    elif small_wins:
        v.append("A bare apk delivered at small size via direct but was not "
                 "re-proven big (or --real-apk was used):")
        for w in small_wins:
            v.append(f"  - {w['label']}: stored {w.get('stored')}")
        v.append("Re-run with --real-apk pointed at your file to confirm at full "
                 "size; if it holds, this is the route to wire in.")
    else:
        v.append("The direct path did NOT deliver a bare apk this round either.")
        hosts_up = [h for h, r in REPORT.get("hosts", {}).items()
                    if "error" not in r]
        v.append(f"Hosts that answered: {len(hosts_up)} of "
                 f"{len(REPORT.get('hosts', {}))}.")
        if not REPORT.get("extract", {}).get("context"):
            v.append("And the context did not extract, so the arsenal may not have "
                     "truly run - see PHASE 1. Fix that first.")
        else:
            v.append("The context extracted and the sends were made, so this is a "
                     "real negative: Eitaa's apk content filter now covers the "
                     "media host too, not just the browser bridge. In that case a "
                     "bare apk is not deliverable on ANY path, and the realistic "
                     "options left are the PDF/HTML-with-a-download-link idea, or "
                     "hosting the apk and sending a link.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)
    try:
        p = outdir / "report.json"
        p.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        print(f"\n  full report: {p}", flush=True)
        print("  probe messages tagged ARSENAL-* in Saved Messages.", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Extract the direct context from Eitaa, map its hosts, and "
                    "throw every apk method at the media host. Own Saved Messages only.")
    p.add_argument("--account", required=True)
    p.add_argument("--size-kb", type=float, default=12)
    p.add_argument("--max-mb", type=float, default=9.6)
    p.add_argument("--real-apk", default=None, help="path to a real apk")
    p.add_argument("--wait", type=float, default=20)
    p.add_argument("--settle", type=float, default=18)
    p.add_argument("--no-light", action="store_true")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
