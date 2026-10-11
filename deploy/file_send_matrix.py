#!/usr/bin/env python3
"""Cross every upload METHOD with every file TYPE, and let the server decide.

Why
---
Sending .zip and .apk is failing. The bot's own path cannot even start on the
current Eitaa build:

    eitaa/bridge_file_send.js, __MKWL_fileInit:
        const AMM = window.appMessagesManager;
        if (!AMM || !AMM.sendFile) return { ok: false, code: "no sendFile" };

`window.appMessagesManager` is one of the globals Eitaa moved into its
SharedWorker, so that guard returns immediately and no upload is attempted. The
apiManager fix did not cover it on purpose: those appXManager globals were
synchronous on the old build and return promises through the worker proxy, so
switching them blindly was the one thing that could break callers silently.

Two questions are open, and guessing either costs a long upload:

  1. WHICH upload mechanism still works on this build?
  2. WHICH (filename, MIME) pair does Eitaa accept for an apk and for a zip?

Question 2 has history here: Eitaa filters the apk MIME
`application/vnd.android.package-archive`, and `direct/apk_mode.py` smuggles it
through as `application/octet-stream` with the real name in
`documentAttributeFilename`. That was proven on the browser-free path. Whether
it holds for each mechanism below is what this measures.

How
---
Breadth first, and the server is the only judge. Every case uploads a small
payload to YOUR OWN Saved Messages with a unique marker in the caption. At the
end the probe reads Saved Messages back ONCE and matches markers, then prints
what the server STORED for each one - mime_type, filename, size - so an
"accepted but rewritten" upload is visible instead of looking like a clean pass.

Methods (only those that exist on this build are run):
    sendFile-global    appMessagesManager.sendFile           the bot's current path
    sendFile-proxy     the same through the worker-backed manager
    saveFilePart       upload.saveFilePart + messages.sendMedia, pure invokeApi
    saveBigFilePart    upload.saveBigFilePart + sendMedia, the >10 MB route
    uploadMedia        messages.uploadMedia, then sendMedia by reference

Types, per method:
    txt    baseline - if this fails the MECHANISM is broken, not the type
    zip    real zip, application/zip
    zip    real zip, application/octet-stream
    zip    real zip named .bin, application/zip
    apk    real apk-shaped zip, application/vnd.android.package-archive  (blocked one)
    apk    application/octet-stream        (what apk_mode.py does)
    apk    application/zip                 (an apk IS a zip, worth asking)
    apk    named .zip, application/zip
    apk    named .apk.txt, text/plain      (does the NAME or the MIME decide?)

Safety
------
* Every send targets YOUR OWN Saved Messages. No contact is ever a target.
* Payloads are generated, tiny by default (--size-kb 8), and marked as probes.
* No project file is modified; the probe injects its own page code.
* Nothing counts as working on a UI reaction - only a server read-back does.

Run on the SERVER, with the bot stopped so the profile is free:

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/file_send_matrix.py --account 989104938987
    systemctl start mkwlsoso-bot

Options:
    --size-kb 8       payload size for the matrix (default 8 KB, keeps it fast)
    --real-mb 9.6     ALSO retry the winning combos at a realistic apk size
    --only zip|apk    restrict the type column
    --methods a,b     restrict the method column (comma separated)
    --settle 20       seconds to wait before the verification read
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import random
import string
import sys
import time
import traceback
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"capabilities": {}, "cases": [], "verified": {}, "verdict": []}
_T0 = time.time()


def banner(t: str) -> None:
    print("\n" + "=" * 78 + f"\n  {t}\n" + "=" * 78, flush=True)


def log(m: str = "") -> None:
    print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)


def ok(m: str) -> None:
    log(f"OK    {m}")


def bad(m: str) -> None:
    log(f"FAIL  {m}")


def info(m: str) -> None:
    log(f"      {m}")


def dump(label: str, obj, indent: str = "        ") -> None:
    info(label)
    for line in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines():
        print(indent + line, flush=True)


async def ev(page, js: str, arg=None, timeout: float = 180.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# ==========================================================================
# payloads - real archives, so a content check is distinguishable from a MIME
# check
# ==========================================================================

def make_zip(size_bytes: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # Random bytes: deflate would crush repetitive filler to nothing and the
        # archive would never reach the size asked for.
        z.writestr("probe.bin", os.urandom(max(1024, size_bytes)))
        z.writestr("readme.txt", "mkwl file-send probe\n")
    return buf.getvalue()


def make_apk(size_bytes: int) -> bytes:
    """A structurally real apk: a zip carrying the entries an apk must have.

    Eitaa has only ever been observed to filter on the MIME, but a probe built
    from random bytes could not tell a content check from a MIME check.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AndroidManifest.xml",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<manifest package="com.mkwl.probe" />\n')
        z.writestr("classes.dex", b"dex\n035\x00" + os.urandom(512))
        z.writestr("resources.arsc", os.urandom(256))
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("assets/payload.bin", os.urandom(max(1024, size_bytes)))
    return buf.getvalue()


def make_txt(size_bytes: int) -> bytes:
    line = "mkwl file-send probe baseline\n"
    return (line * max(1, size_bytes // len(line))).encode()


APK_MIME = "application/vnd.android.package-archive"
ZIP_MIME = "application/zip"
OCTET = "application/octet-stream"


def build_cases(size_bytes: int, only: str | None) -> list[dict]:
    """The type column: (label, kind, filename, mime).

    Each row answers one question, and the questions are chosen so the answers
    separate causes: name vs MIME vs content vs mechanism.
    """
    zip_bytes = make_zip(size_bytes)
    apk_bytes = make_apk(size_bytes)
    txt_bytes = make_txt(size_bytes)
    rows = [
        # Baseline first: a mechanism that fails here is broken regardless of type.
        ("txt-baseline", "txt", "probe.txt", "text/plain", txt_bytes,
         "baseline - isolates a broken mechanism from a refused type"),

        ("zip-real-mime", "zip", "probe.zip", ZIP_MIME, zip_bytes,
         "the normal way to send a zip"),
        ("zip-octet", "zip", "probe.zip", OCTET, zip_bytes,
         "same bytes and name, generic MIME - does the MIME matter for zip?"),
        ("zip-named-bin", "zip", "probe.bin", ZIP_MIME, zip_bytes,
         "zip MIME under a neutral name - does the NAME matter?"),

        ("apk-real-mime", "apk", "probe.apk", APK_MIME, apk_bytes,
         "the MIME this repo already proved Eitaa filters"),
        ("apk-octet", "apk", "probe.apk", OCTET, apk_bytes,
         "what direct/apk_mode.py does - real name, generic MIME"),
        ("apk-zip-mime", "apk", "probe.apk", ZIP_MIME, apk_bytes,
         "an apk IS a zip, so declare it as one"),
        ("apk-named-zip", "apk", "probe.zip", ZIP_MIME, apk_bytes,
         "apk bytes fully disguised - if THIS fails the check is on content"),
        ("apk-named-txt", "apk", "probe.apk.txt", "text/plain", apk_bytes,
         "name says txt, bytes are an apk - name or MIME?"),
    ]
    if only:
        keep = {"zip": ("txt", "zip"), "apk": ("txt", "apk")}.get(only, None)
        if keep:
            rows = [r for r in rows if r[1] in keep]
    return [{"label": r[0], "kind": r[1], "name": r[2], "mime": r[3],
             "bytes": r[4], "why": r[5]} for r in rows]


# ==========================================================================
# the page side: one function per upload mechanism
# ==========================================================================

# Injected once. Each method returns {ok, code, detail, ms} and NEVER throws, so
# one dead mechanism cannot end the run. The marker travels in the caption and
# is what the server read-back matches on.
UPLOAD_JS = r"""
(() => {
  if (window.__MKWLFM) return;
  const FM = {};
  window.__MKWLFM = FM;

  function API() {
    try { if (window.__MKWL_api) return window.__MKWL_api(); } catch (e) {}
    return window.apiManager || null;
  }
  function MGR(name) {
    try { if (window.__MKWL_mgr) return window.__MKWL_mgr(name); } catch (e) {}
    return window[name] || null;
  }
  function errCode(e) {
    try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return "ERR"; }
  }
  function randId() {
    // Telegram wants a random long; two 32-bit halves as a decimal string is
    // what tweb itself does.
    return String(Math.floor(Math.random() * 0xffffffff)) +
           String(Math.floor(Math.random() * 0xffffffff));
  }
  function bytesOf(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  FM.capabilities = function () {
    const AM = API();
    const out = {
      api: !!(AM && AM.invokeApi),
      api_source: window.__MKWL_apiSource || null,
      appMessagesManager_global: !!(window.appMessagesManager),
      appMessagesManager_sendFile_global:
        !!(window.appMessagesManager && window.appMessagesManager.sendFile),
      appMessagesManager_proxy: false,
      appUsersManager_global: !!window.appUsersManager,
      appPeersManager_global: !!window.appPeersManager
    };
    try {
      const M = MGR("appMessagesManager");
      out.appMessagesManager_proxy = !!M;
      // A Proxy answers "function" for any name, so this is not proof - only a
      // real call is, which is what the matrix does.
      out.appMessagesManager_sendFile_proxy = !!(M && typeof M.sendFile === "function");
    } catch (e) {}
    return out;
  };

  FM.selfId = async function () {
    const c = [];
    try { if (window.rootScope && window.rootScope.getMyId) c.push(await window.rootScope.getMyId()); } catch (e) {}
    try {
      const M = MGR("appUsersManager");
      if (M && M.getSelf) { const s = await M.getSelf(); if (s && s.id != null) c.push(s.id); }
    } catch (e) {}
    try { if (window.appPeersManager) c.push(window.appPeersManager.peerId); } catch (e) {}
    for (const x of c) { if (x != null && +x !== 0 && !isNaN(+x)) return +x; }
    return null;
  };

  // ---- method 1 and 2: appMessagesManager.sendFile -----------------------
  // This is the path bridge_file_send.js uses. `which` picks the global (the
  // bot's current code) or the worker-backed manager.
  FM.sendFile = async function (a, which) {
    const t0 = Date.now();
    const M = which === "proxy" ? MGR("appMessagesManager") : window.appMessagesManager;
    if (!M) return { ok: false, code: "no appMessagesManager (" + which + ")" };
    if (typeof M.sendFile !== "function") return { ok: false, code: "no sendFile (" + which + ")" };
    const sid = await FM.selfId();
    if (sid == null) return { ok: false, code: "no self id" };
    let file;
    try {
      file = new File([bytesOf(a.b64)], a.name, { type: a.mime });
    } catch (e) { return { ok: false, code: "build_file:" + errCode(e) }; }
    try {
      // Object form first (newer tweb), positional second (older).
      try {
        await M.sendFile({ peerId: sid, file: file, caption: a.marker });
        return { ok: true, code: "sendFile(object)", ms: Date.now() - t0 };
      } catch (e1) {
        await M.sendFile(sid, file, { caption: a.marker });
        return { ok: true, code: "sendFile(positional)", ms: Date.now() - t0 };
      }
    } catch (e) {
      return { ok: false, code: errCode(e), ms: Date.now() - t0 };
    }
  };

  // ---- the raw route: upload the bytes ourselves, then sendMedia ----------
  // No manager involved at all, so it survives anything Eitaa does to its
  // globals. 512 KB parts is what tweb uses.
  async function saveParts(AM, bytes, big) {
    const fileId = randId();
    const PART = 512 * 1024;
    const total = Math.max(1, Math.ceil(bytes.length / PART));
    for (let i = 0; i < total; i++) {
      const slice = bytes.subarray(i * PART, Math.min((i + 1) * PART, bytes.length));
      const method = big ? "upload.saveBigFilePart" : "upload.saveFilePart";
      const params = big
        ? { file_id: fileId, file_part: i, file_total_parts: total, bytes: slice }
        : { file_id: fileId, file_part: i, bytes: slice };
      const r = await AM.invokeApi(method, params);
      if (r === false) return { ok: false, code: method + " returned boolFalse", part: i };
    }
    return { ok: true, file_id: fileId, parts: total };
  }

  function inputFile(fileId, parts, name, big) {
    return big
      ? { _: "inputFileBig", id: fileId, parts: parts, name: name }
      : { _: "inputFile", id: fileId, parts: parts, name: name, md5_checksum: "" };
  }

  function uploadedDoc(file, name, mime) {
    return {
      _: "inputMediaUploadedDocument",
      file: file,
      mime_type: mime,
      // The real name rides here. direct/apk_mode.py relies on exactly this:
      // the MIME can be generic while the recipient still gets "x.apk".
      attributes: [{ _: "documentAttributeFilename", file_name: name }]
    };
  }

  FM.rawUpload = async function (a, big) {
    const t0 = Date.now();
    const AM = API();
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    const bytes = bytesOf(a.b64);
    let up;
    try { up = await saveParts(AM, bytes, big); }
    catch (e) { return { ok: false, code: "upload:" + errCode(e), ms: Date.now() - t0 }; }
    if (!up.ok) return { ok: false, code: up.code, ms: Date.now() - t0 };
    const media = uploadedDoc(inputFile(up.file_id, up.parts, a.name, big), a.name, a.mime);
    try {
      const res = await AM.invokeApi("messages.sendMedia", {
        peer: { _: "inputPeerSelf" }, media: media,
        message: a.marker, random_id: randId() });
      return { ok: true, code: big ? "saveBigFilePart+sendMedia" : "saveFilePart+sendMedia",
               parts: up.parts, ms: Date.now() - t0,
               reply: res && res._ ? res._ : null };
    } catch (e) {
      return { ok: false, code: "sendMedia:" + errCode(e), parts: up.parts,
               ms: Date.now() - t0, upload_ok: true };
    }
  };

  // ---- messages.uploadMedia: let the server materialise the document first,
  // then send it by reference. A different server-side code path, so it can
  // accept what sendMedia refuses (or refuse earlier, which is just as useful).
  FM.uploadMedia = async function (a) {
    const t0 = Date.now();
    const AM = API();
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    const bytes = bytesOf(a.b64);
    let up;
    try { up = await saveParts(AM, bytes, false); }
    catch (e) { return { ok: false, code: "upload:" + errCode(e), ms: Date.now() - t0 }; }
    if (!up.ok) return { ok: false, code: up.code, ms: Date.now() - t0 };
    let doc;
    try {
      const r = await AM.invokeApi("messages.uploadMedia", {
        peer: { _: "inputPeerSelf" },
        media: uploadedDoc(inputFile(up.file_id, up.parts, a.name, false), a.name, a.mime) });
      doc = r && r.document;
      if (!doc) return { ok: false, code: "uploadMedia gave no document",
                         ms: Date.now() - t0 };
    } catch (e) {
      return { ok: false, code: "uploadMedia:" + errCode(e), ms: Date.now() - t0,
               upload_ok: true };
    }
    try {
      const res = await AM.invokeApi("messages.sendMedia", {
        peer: { _: "inputPeerSelf" },
        media: { _: "inputMediaDocument",
                 id: { _: "inputDocument", id: doc.id, access_hash: doc.access_hash,
                       file_reference: doc.file_reference } },
        message: a.marker, random_id: randId() });
      return { ok: true, code: "uploadMedia+sendMedia", ms: Date.now() - t0,
               stored_mime: doc.mime_type || null,
               reply: res && res._ ? res._ : null };
    } catch (e) {
      return { ok: false, code: "sendMedia(ref):" + errCode(e), ms: Date.now() - t0,
               uploadMedia_ok: true, stored_mime: doc.mime_type || null };
    }
  };

  // ---- the verification read: what the SERVER actually stored --------------
  FM.readBack = async function (limit) {
    const AM = API();
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    try {
      const h = await AM.invokeApi("messages.getHistory", {
        peer: { _: "inputPeerSelf" }, offset_id: 0, offset_date: 0, add_offset: 0,
        limit: limit || 60, max_id: 0, min_id: 0, hash: 0 });
      const out = [];
      for (const m of (h && h.messages) || []) {
        if (!m) continue;
        const d = m.media && m.media.document;
        let fname = null;
        try {
          for (const at of (d && d.attributes) || []) {
            if (at && at._ === "documentAttributeFilename") fname = at.file_name;
          }
        } catch (e) {}
        out.push({
          id: m.id,
          message: (m.message || "").slice(0, 120),
          has_document: !!d,
          mime: d ? (d.mime_type || null) : null,
          size: d ? (d.size != null ? String(d.size) : null) : null,
          file_name: fname
        });
      }
      return { ok: true, messages: out };
    } catch (e) {
      return { ok: false, code: "getHistory:" + errCode(e) };
    }
  };
})()
"""


# ==========================================================================
# the matrix
# ==========================================================================

#: method name -> (page call, why it is in the list)
METHODS = {
    "sendFile-global": ("FM.sendFile(a, 'global')",
                        "the path bridge_file_send.js uses today"),
    "sendFile-proxy": ("FM.sendFile(a, 'proxy')",
                       "the same manager, reached through the worker proxy"),
    "saveFilePart": ("FM.rawUpload(a, false)",
                     "pure invokeApi - no manager, so no global can break it"),
    "saveBigFilePart": ("FM.rawUpload(a, true)",
                        "the >10 MB route; a real apk needs it"),
    "uploadMedia": ("FM.uploadMedia(a)",
                    "server materialises the document first, then send by ref"),
}


def marker(method: str, label: str) -> str:
    rnd = "".join(random.choice(string.ascii_uppercase + string.digits)
                  for _ in range(6))
    return f"MKWLFM-{rnd}-{method}-{label}"


async def run_case(page, method: str, case: dict, timeout: float) -> dict:
    import base64
    mk = marker(method, case["label"])
    arg = {"b64": base64.b64encode(case["bytes"]).decode("ascii"),
           "name": case["name"], "mime": case["mime"], "marker": mk}
    call = METHODS[method][0]
    js = f"async (a) => {{ const FM = window.__MKWLFM; try {{ return await {call}; }} "
    js += "catch (e) { return { ok: false, code: 'threw:' + String(e) }; } }"
    res, err = await ev(page, js, arg, timeout=timeout)
    out = {"method": method, "label": case["label"], "name": case["name"],
           "mime": case["mime"], "bytes": len(case["bytes"]), "marker": mk,
           "why": case["why"], "result": res or {"ok": False, "code": err}}
    r = out["result"]
    if r.get("ok"):
        ok(f"{method:<16} {case['label']:<15} accepted the call "
           f"({r.get('code')}, {r.get('ms', '?')}ms)")
    else:
        bad(f"{method:<16} {case['label']:<15} {str(r.get('code'))[:90]}")
    return out


async def run(args) -> int:
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"file_matrix_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver, api_info
    from eitaa.driver import EitaaDriver

    banner("STEP 1  -  open the account and prove the session")
    light = False if args.no_light else None
    t = time.time()
    session = await BrowserSession(args.account, headed=args.headed,
                                   light_assets=light).start()
    ok(f"browser up in {time.time() - t:.1f}s")
    page = session.page
    try:
        resp = await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                               timeout=120_000)
        info(f"HTTP {resp.status if resp else '?'}; letting the app boot "
             f"{args.wait:.0f}s")
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)
        info(f"api: {await api_info(page)}")

        driver = EitaaDriver(session)
        st = await driver.login_state()
        REPORT["login_state"] = st
        if not st.get("logged_in"):
            bad(f"this account is not logged in ({st}) - nothing can be uploaded")
            REPORT["verdict"].append(
                "Not logged in, so the matrix would measure nothing. Log the "
                "account in first (deploy/login_e2e.py).")
            summarise(outdir)
            return 2
        ok(f"logged in: my_id={st.get('my_id')} (via {st.get('how')})")

        banner("STEP 2  -  which upload mechanisms exist on this build")
        _, err = await ev(page, f"() => {{ {UPLOAD_JS} }}", timeout=30)
        if err:
            bad(f"could not inject the probe code - {err}")
            return 2
        caps, err = await ev(page, "() => window.__MKWLFM.capabilities()", timeout=30)
        REPORT["capabilities"] = caps or {"error": err}
        if caps:
            dump("capabilities:", caps)
            if not caps.get("appMessagesManager_sendFile_global"):
                bad("appMessagesManager.sendFile is MISSING - this is exactly the "
                    "guard bridge_file_send.js returns on, so the bot's upload "
                    "cannot start at all on this build")
            if caps.get("api"):
                ok(f"invokeApi available via {caps.get('api_source')} - the raw "
                   f"upload route does not depend on any manager")
        sid, _ = await ev(page, "async () => await window.__MKWLFM.selfId()", timeout=30)
        REPORT["self_id"] = sid
        (ok if sid else bad)(f"self id for Saved Messages: {sid}")

        banner("STEP 3  -  the matrix")
        size = int(args.size_kb * 1024)
        cases = build_cases(size, args.only)
        wanted = [m.strip() for m in args.methods.split(",")] if args.methods \
            else list(METHODS)
        wanted = [m for m in wanted if m in METHODS]
        info(f"{len(wanted)} method(s) x {len(cases)} type(s) = "
             f"{len(wanted) * len(cases)} cases, {args.size_kb} KB each")
        for m in wanted:
            info(f"  {m:<16} {METHODS[m][1]}")
        print("", flush=True)

        for m in wanted:
            # Baseline first within each method: if txt fails the mechanism is
            # broken and the remaining types would only repeat that one fact.
            for case in cases:
                rec = await run_case(page, m, case, args.case_timeout)
                REPORT["cases"].append(rec)
                if case["label"] == "txt-baseline" and not rec["result"].get("ok"):
                    bad(f"{m}: the baseline failed, so this mechanism is "
                        f"unavailable - skipping its remaining types")
                    break
                await asyncio.sleep(float(args.pace))

        banner(f"STEP 4  -  what the SERVER stored  (waiting {args.settle:.0f}s first)")
        info("a UI reaction proves nothing; this read-back is the only judge")
        await asyncio.sleep(float(args.settle))
        back, err = await ev(page, "async () => await window.__MKWLFM.readBack(80)",
                             timeout=120)
        REPORT["readback"] = back or {"error": err}
        msgs = (back or {}).get("messages") or []
        info(f"{len(msgs)} message(s) read from Saved Messages")
        by_marker = {}
        for m in msgs:
            txt = m.get("message") or ""
            if "MKWLFM-" in txt:
                for rec in REPORT["cases"]:
                    if rec["marker"] in txt:
                        by_marker[rec["marker"]] = m
        REPORT["verified"] = by_marker
        for rec in REPORT["cases"]:
            found = by_marker.get(rec["marker"])
            rec["delivered"] = bool(found)
            rec["stored"] = found
        await verify_report()

        if args.real_mb:
            banner(f"STEP 5  -  retry what worked at {args.real_mb} MB")
            winners = [r for r in REPORT["cases"]
                       if r.get("delivered") and r["label"] != "txt-baseline"]
            if not winners:
                info("nothing worked at the small size, so a big upload would "
                     "only waste minutes - skipped")
            else:
                big = int(float(args.real_mb) * 1024 * 1024)
                seen = set()
                for w in winners:
                    key = (w["method"], w["label"])
                    if key in seen:
                        continue
                    seen.add(key)
                    kind = next(c for c in cases if c["label"] == w["label"])
                    payload = (make_apk(big) if kind["kind"] == "apk"
                               else make_zip(big) if kind["kind"] == "zip"
                               else make_txt(big))
                    case = dict(kind)
                    case["bytes"] = payload
                    case["label"] = kind["label"] + f"-{args.real_mb}MB"
                    info(f"{w['method']} / {case['label']}: "
                         f"{len(payload) / 1e6:.1f} MB")
                    rec = await run_case(page, w["method"], case,
                                         max(args.case_timeout, 600))
                    REPORT["cases"].append(rec)
                await asyncio.sleep(float(args.settle))
                back2, _ = await ev(page,
                                    "async () => await window.__MKWLFM.readBack(40)",
                                    timeout=120)
                for m in ((back2 or {}).get("messages") or []):
                    txt = m.get("message") or ""
                    for rec in REPORT["cases"]:
                        if rec["marker"] in txt:
                            rec["delivered"] = True
                            rec["stored"] = m

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


async def verify_report() -> None:
    """Print the delivered/refused split with what the server stored."""
    for rec in REPORT["cases"]:
        s = rec.get("stored") or {}
        if rec.get("delivered"):
            changed = []
            if s.get("mime") and s["mime"] != rec["mime"]:
                changed.append(f"MIME {rec['mime']} -> {s['mime']}")
            if s.get("file_name") and s["file_name"] != rec["name"]:
                changed.append(f"name {rec['name']} -> {s['file_name']}")
            note = ("  [server rewrote: " + "; ".join(changed) + "]") if changed else ""
            ok(f"DELIVERED  {rec['method']:<16} {rec['label']:<15} "
               f"stored mime={s.get('mime')} name={s.get('file_name')} "
               f"size={s.get('size')}{note}")
        elif rec["result"].get("ok"):
            bad(f"SILENT     {rec['method']:<16} {rec['label']:<15} "
                f"the call succeeded but the server has no such message - "
                f"accepted then dropped")
        else:
            bad(f"REFUSED    {rec['method']:<16} {rec['label']:<15} "
                f"{str(rec['result'].get('code'))[:70]}")


def summarise(outdir) -> None:
    cases = REPORT["cases"]
    banner("MATRIX")
    if cases:
        methods = []
        for r in cases:
            if r["method"] not in methods:
                methods.append(r["method"])
        labels = []
        for r in cases:
            if r["label"] not in labels:
                labels.append(r["label"])
        w = max(len(l) for l in labels) + 1
        head = "  " + "type".ljust(w) + "".join(m[:15].ljust(17) for m in methods)
        print(head, flush=True)
        print("  " + "-" * (len(head) - 2), flush=True)
        for lab in labels:
            row = "  " + lab.ljust(w)
            for m in methods:
                rec = next((r for r in cases
                            if r["method"] == m and r["label"] == lab), None)
                if rec is None:
                    cell = "-"
                elif rec.get("delivered"):
                    cell = "DELIVERED"
                elif rec["result"].get("ok"):
                    cell = "silent-drop"
                else:
                    cell = "refused"
                row += cell.ljust(17)
            print(row, flush=True)
        print("\n  DELIVERED  = the server really has it (the only pass)", flush=True)
        print("  silent-drop= the call succeeded, the server has nothing", flush=True)
        print("  refused    = the call itself failed, with the reason above", flush=True)

    banner("VERDICT")
    v = REPORT["verdict"]
    caps = REPORT.get("capabilities") or {}
    delivered = [r for r in cases if r.get("delivered")]
    base_ok = [r for r in delivered if r["label"] == "txt-baseline"]
    apk_ok = [r for r in delivered if r["label"].startswith("apk")]
    zip_ok = [r for r in delivered if r["label"].startswith("zip")]

    if caps and not caps.get("appMessagesManager_sendFile_global"):
        v.append("CONFIRMED for the bot's current failure: "
                 "window.appMessagesManager.sendFile does not exist on this "
                 "build, and eitaa/bridge_file_send.js returns 'no sendFile' on "
                 "that exact guard before uploading anything. Any file send "
                 "through the bridge is dead until that path changes.")
    if not cases:
        v.append("No case ran - see the errors above.")
    elif not base_ok:
        v.append("Not even the txt baseline was delivered by any mechanism, so "
                 "this is not about file TYPE. Either the session cannot upload "
                 "at all or every mechanism is unavailable; the refusal reasons "
                 "in STEP 3 say which.")
    else:
        working = sorted({r["method"] for r in base_ok})
        v.append(f"Working upload mechanism(s): {working}. That is what the "
                 f"bridge file path should be built on.")
        if zip_ok:
            combos = sorted({f"{r['method']}/{r['label']}" for r in zip_ok})
            v.append(f"ZIP is deliverable: {combos}")
        else:
            v.append("ZIP was NOT delivered by any mechanism that otherwise "
                     "works - so zip is being refused on its own merits, not "
                     "because the mechanism is broken.")
        if apk_ok:
            combos = sorted({f"{r['method']}/{r['label']}" for r in apk_ok})
            v.append(f"APK is deliverable: {combos}")
            octet = [r for r in apk_ok if r["label"] == "apk-octet"]
            real = [r for r in apk_ok if r["label"] == "apk-real-mime"]
            if octet and not real:
                v.append("And it is the MIME that decides, exactly as "
                         "direct/apk_mode.py already assumed: the real apk MIME "
                         "is refused while the same bytes under "
                         "application/octet-stream go through. Turning "
                         "MKWL_APK_OCTET on is the whole apk fix.")
            elif real:
                v.append("Note: the real apk MIME was accepted here, which "
                         "contradicts the earlier finding - re-check before "
                         "relying on apk_mode's rewrite.")
        else:
            v.append("APK was not delivered by any mechanism. The per-case "
                     "reasons separate name from MIME from content: if "
                     "apk-named-zip also failed, the check is on the BYTES, and "
                     "no MIME rewrite can help.")
        silent = [r for r in cases if r["result"].get("ok") and not r.get("delivered")]
        if silent:
            v.append(f"{len(silent)} case(s) were accepted and then silently "
                     f"dropped. Those are the dangerous ones: the bot would "
                     f"report them as sent. Any send path must verify with a "
                     f"read-back, not trust the call.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)

    try:
        p = outdir / "report.json"
        p.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        print(f"\n  full report: {p}", flush=True)
        print(f"  (the probe messages stay in your Saved Messages, tagged "
              f"MKWLFM-* so you can delete them)", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Cross every upload method with every file type, verified "
                    "by a server read-back. Sends only to your own Saved Messages.")
    p.add_argument("--account", required=True, help="a LOGGED-IN account id")
    p.add_argument("--size-kb", type=float, default=8,
                   help="payload size for the matrix (default 8 KB)")
    p.add_argument("--real-mb", type=float, default=0,
                   help="also retry the winning combos at this size, in MB")
    p.add_argument("--only", choices=["zip", "apk"], default=None,
                   help="restrict the type column")
    p.add_argument("--methods", default=None,
                   help="comma separated subset of: " + ",".join(METHODS))
    p.add_argument("--wait", type=float, default=20,
                   help="seconds to let the app boot (default 20)")
    p.add_argument("--pace", type=float, default=1.5,
                   help="seconds between cases (default 1.5)")
    p.add_argument("--settle", type=float, default=20,
                   help="seconds before the verification read (default 20)")
    p.add_argument("--case-timeout", type=float, default=120,
                   help="per-case ceiling in seconds (default 120)")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-light", action="store_true")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
