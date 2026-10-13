#!/usr/bin/env python3
"""APK explorer — map what this account CAN do, then find a way to send an .apk.

WHY THIS REPLACES apk_probe.py
------------------------------
apk_probe.py answered one question (which MIME?) and answered it slowly: every
failing upload burned the driver's default locate budget (45s + 25s/MB) plus a
30s late-check, so eight cases took ten minutes. Worse, its verdict trusted
"the preview popup closed" as proof of delivery for the UI path -- and on a
RESTRICTED account that is exactly the lie that wasted a night: the popup closes,
the script says SENT, and Eitaa silently refuses the message.

So this one is built the other way round:

  * BREADTH FIRST. More probes, each cheap. It maps the account's capabilities
    and the page's real API surface before it uploads anything, then crosses
    every send METHOD with every content TYPE. Text, txt, zip, png and apk are
    all tested -- not because text matters, but because the apk answer only means
    something against a baseline of what else works.

  * FAST. Probe uploads pass locate_timeout explicitly (default 12s, not 47s),
    payloads are 8 KB, and there is ONE delayed verification sweep at the end
    instead of a 30s wait per case. A full run is a couple of minutes.

  * NOTHING IS "SENT" UNTIL THE SERVER SAYS SO. Every attempt carries a unique
    marker in its caption. At the end the probe reads Saved Messages back from
    the server ONCE and matches markers. A method is only reported as working if
    its message is actually there, whatever the UI did.

  * IT STOPS EARLY WHEN THE ANSWER IS ALREADY IN. If the account is restricted,
    running eighteen uploads proves nothing -- it says so and stops.

    cd ~/Mkwlsoso
    DISPLAY=:99 .venv/bin/python deploy/apk_explore.py --account 989991048633
    # everything, including the slower methods:
    DISPLAY=:99 .venv/bin/python deploy/apk_explore.py --account 989991048633 --deep

Delivers ONLY to the account's own Saved Messages, with dummy files.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import mimetypes
import os
import secrets
import sys
import time
import zipfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BLOCKED_APK_MIME = "application/vnd.android.package-archive"
OCTET = "application/octet-stream"
HOOKS_JS = os.path.join(_ROOT, "capture", "hooks.js")

# --------------------------------------------------------------------------
# tiny logger
# --------------------------------------------------------------------------


class Log:
    def __init__(self, outdir: Path) -> None:
        self.dir = outdir
        (self.dir / "net").mkdir(parents=True, exist_ok=True)
        self._f = (self.dir / "explore.log").open("a", encoding="utf-8")
        self.rows: list[dict] = []

    def say(self, s: str = "") -> None:
        print(s, flush=True)
        try:
            self._f.write(s + "\n")
            self._f.flush()
        except Exception:  # noqa: BLE001
            pass

    def head(self, s: str) -> None:
        self.say("")
        self.say("=" * 72)
        self.say(s)
        self.say("=" * 72)

    def rec(self, row: dict) -> None:
        self.rows.append(row)
        try:
            with (self.dir / "attempts.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def blob(self, name: str, payload) -> None:
        try:
            (self.dir / "net" / name).write_text(
                json.dumps(payload, ensure_ascii=False, default=str, indent=1),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# payloads
# --------------------------------------------------------------------------


def apk_bytes(size: int) -> bytes:
    """A genuine zip carrying the entries that make a file an apk.

    Random bytes would sail past a content sniffer and we would conclude the
    wrong thing, so the dummy apk is apk-shaped for real.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00dummy")
        z.writestr("classes.dex", b"dex\n035\x00" + b"\x00" * 32)
        z.writestr("resources.arsc", b"\x02\x00\x0c\x00")
        pad = max(0, size - buf.tell() - 200)
        if pad:
            z.writestr("assets/pad", b"\x00" * pad)
    return buf.getvalue()


def zip_bytes(size: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("readme.txt", b"probe")
        pad = max(0, size - buf.tell() - 150)
        if pad:
            z.writestr("pad.bin", b"\x00" * pad)
    return buf.getvalue()


def png_bytes(size: int) -> bytes:
    """A minimal valid 1x1 PNG, padded. Photos take a different Eitaa path."""
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082")
    return png + b"\x00" * max(0, size - len(png))


def txt_bytes(size: int) -> bytes:
    return (b"probe " * 64)[:max(16, size)]


# --------------------------------------------------------------------------
# page introspection: what does this build actually offer?
# --------------------------------------------------------------------------

#: Discover the upload API surface instead of assuming it. The whole apk story
#: turned out to hinge on our CALL not matching this build, so the shape of these
#: functions is primary evidence, not trivia.
SURFACE_JS = r"""
() => {
  const out = {};
  const put = (k, fn) => { try { out[k] = fn(); } catch (e) { out[k] = "ERR:" + (e && e.message); } };
  const sig = (f) => {
    if (typeof f !== "function") return null;
    const s = String(f);
    return { arity: f.length, head: s.slice(0, 220).replace(/\s+/g, " ") };
  };

  const AMM = window.appMessagesManager, AM = window.apiManager;
  put("has_apiManager", () => !!AM);
  put("has_appMessagesManager", () => !!AMM);
  put("layer", () => window.Config && (window.Config.Schema && window.Config.Schema.layer
        || window.Config.LAYER) || null);

  if (AMM) {
    for (const n of ["sendFile", "sendText", "sendOther", "sendMessage", "uploadFile"]) {
      put("AMM." + n, () => sig(AMM[n]));
    }
    put("AMM_upload_like_keys", () => Object.keys(AMM)
        .filter(k => /file|upload|media|doc/i.test(k)).slice(0, 40));
  }
  // Whatever the app uses for uploads: a manager, a worker, or both.
  for (const g of ["appDocsManager", "appPhotosManager", "apiFileManager",
                   "appDownloadManager", "appUploadManager"]) {
    put("global." + g, () => !!window[g]);
  }
  put("apiFileManager.upload", () => window.apiFileManager && sig(window.apiFileManager.upload));
  put("our_bridge", () => ({
    fileInit: typeof window.__MKWL_fileInit,
    fileSend: typeof window.__MKWL_fileSend,
    send: typeof window.__MKWL_send
  }));
  return out;
}
"""

#: Anything the account or client says about being restricted. Read BEFORE
#: uploading: on a restricted account every upload result is noise.
RESTRICTION_JS = r"""
async () => {
  const out = {};
  const AM = window.apiManager;
  const put = (k, v) => { out[k] = v; };
  if (!AM || !AM.invokeApi) return { error: "no invokeApi" };
  try {
    const full = await AM.invokeApi("users.getFullUser", {
      id: { _: "inputUserSelf" } });
    const u = full && (full.user || (full.users && full.users[0]));
    const fu = full && full.full_user;
    put("self", u ? { id: u.id, phone: u.phone,
                      restricted: !!(u.pFlags && u.pFlags.restricted),
                      deleted: !!(u.pFlags && u.pFlags.deleted),
                      restriction_reason: u.restriction_reason || null } : null);
    put("full_flags", fu ? Object.keys(fu.pFlags || {}) : null);
  } catch (e) { put("getFullUser_error", String(e && (e.message || e.type) || e)); }
  try {
    const cfg = await AM.invokeApi("help.getConfig", {});
    put("config", cfg ? { test_mode: !!cfg.test_mode,
        blocked_mode: !!(cfg.pFlags && cfg.pFlags.blocked_mode) } : null);
  } catch (e) { put("getConfig_error", String(e && (e.message || e.type) || e)); }
  return out;
}
"""

#: Read Saved Messages back from the SERVER. The only definition of "sent".
HISTORY_JS = r"""
async (limit) => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
  try {
    const h = await AM.invokeApi("messages.getHistory", {
      peer: { _: "inputPeerSelf" }, offset_id: 0, offset_date: 0, add_offset: 0,
      limit: limit || 60, max_id: 0, min_id: 0, hash: 0 });
    return { ok: true, items: ((h && h.messages) || []).map(m => {
      let fname = null;
      try { for (const a of (((m.media||{}).document||{}).attributes||[]))
              if (a.file_name) fname = a.file_name; } catch (e) {}
      return { id: m.id, message: (m.message || "").slice(0, 120),
               hasDoc: !!(m.media && m.media.document),
               hasPhoto: !!(m.media && m.media.photo),
               mime: ((m.media||{}).document||{}).mime_type || null,
               size: ((m.media||{}).document||{}).size || null,
               name: fname };
    }) };
  } catch (e) { return { ok: false, code: String(e && (e.message||e.type) || e) }; }
}
"""

ERRORS_JS = r"""
() => {
  if (!window.__MKWL_pageErrors) {
    window.__MKWL_pageErrors = [];
    const p = (o) => { try { if (window.__MKWL_pageErrors.length < 300) window.__MKWL_pageErrors.push(o); } catch (e) {} };
    window.addEventListener("error", e => p({ t: "error", msg: String(e.message).slice(0,200) }));
    window.addEventListener("unhandledrejection", e => p({ t: "rej",
      msg: String((e.reason && (e.reason.message || e.reason)) || "").slice(0,200) }));
  }
  const out = window.__MKWL_pageErrors.slice();
  window.__MKWL_pageErrors.length = 0;   // drain, so each attempt stands alone
  return out;
}
"""


# --------------------------------------------------------------------------
# the attempt matrix
# --------------------------------------------------------------------------


def matrix(deep: bool, size_kb: int) -> list[dict]:
    """Method x type. Cheap and broad: every row is a few KB and ~12s worst case.

    Ordered so the cheapest, most diagnostic rows run first: a text send needs no
    upload at all, and if THAT fails the account is the story and nothing about
    apk matters.
    """
    n = size_kb * 1024
    rows: list[dict] = [
        # --- no upload at all: is this account able to send anything? ---
        dict(id="text", method="text", kind="text", name="-", mime="-",
             why="BASELINE: can this account send a message at all?"),

        # --- the bot's own upload path, by type ---
        dict(id="api-txt", method="api", kind="txt", name="probe.txt",
             mime="text/plain", size=n, why="API path, smallest possible document"),
        dict(id="api-zip", method="api", kind="zip", name="probe.zip",
             mime="application/zip", size=n, why="API path, ordinary archive"),
        dict(id="api-apk-octet", method="api", kind="apk", name="probe.apk",
             mime=OCTET, size=n, why="API path, apk as generic binary (the fix)"),
        dict(id="api-apk-real", method="api", kind="apk", name="probe.apk",
             mime=BLOCKED_APK_MIME, size=n,
             why="API path, apk with its real MIME (documented as blocked)"),

        # --- the human path, by type. A different mechanism entirely. ---
        dict(id="ui-txt", method="ui-doc", kind="txt", name="probe.txt",
             mime="text/plain", size=n, why="UI attach, document"),
        dict(id="ui-zip", method="ui-doc", kind="zip", name="probe.zip",
             mime="application/zip", size=n, why="UI attach, archive"),
        dict(id="ui-apk", method="ui-doc", kind="apk", name="probe.apk",
             mime=OCTET, size=n, why="UI attach, apk -- THE ONE THAT LOOKED OK"),
    ]
    if deep:
        rows += [
            # Photos go through a different Eitaa code path than documents.
            dict(id="ui-png-photo", method="ui-photo", kind="png",
                 name="probe.png", mime="image/png", size=n,
                 why="UI attach as PHOTO: a different path inside Eitaa"),
            dict(id="ui-apk-photo", method="ui-photo", kind="apk",
                 name="probe.apk", mime=OCTET, size=n,
                 why="apk through the PHOTO path (expected to refuse; tells us "
                     "whether the two paths validate differently)"),
            # Browser-free, no page involved at all.
            dict(id="direct-apk", method="direct", kind="apk", name="probe.apk",
                 mime=OCTET, size=n,
                 why="BROWSER-FREE path: bypasses the page entirely"),
            dict(id="direct-zip", method="direct", kind="zip", name="probe.zip",
                 mime="application/zip", size=n, why="browser-free control"),
        ]
    return rows


# --------------------------------------------------------------------------
# explorer
# --------------------------------------------------------------------------


class Explorer:
    def __init__(self, log: Log, account: str, locate: float) -> None:
        self.log = log
        self.account = account
        self.locate = locate
        self.markers: dict[str, dict] = {}   # marker -> attempt row

    async def _errors(self, page) -> list:
        try:
            return await page.evaluate(ERRORS_JS) or []
        except Exception:  # noqa: BLE001
            return []

    async def _net(self, page, tag: str) -> dict:
        recs: list[dict] = []
        try:
            from capture import deep as _deep
            await _deep.pull_hooks(page, lambda e: recs.append(e))
        except Exception:  # noqa: BLE001
            try:
                recs = list(await page.evaluate(
                    "() => (window.__MKWL_dump ? window.__MKWL_dump() : [])") or [])
            except Exception:  # noqa: BLE001
                recs = []
        if recs:
            self.log.blob(f"{tag}.json", recs)
        return {"records": len(recs)}

    def _payload(self, kind: str, size: int) -> bytes:
        return {"apk": apk_bytes, "zip": zip_bytes,
                "png": png_bytes, "txt": txt_bytes}[kind](size)

    async def attempt(self, drv, page, peer: str, row: dict,
                      tmp: Path) -> dict:
        marker = "PRB" + secrets.token_hex(4).upper()
        rec = dict(row)
        rec["marker"] = marker
        rec["ts"] = time.time()
        await self._errors(page)          # drain, so errors below are ours
        await self._net(page, f"{row['id']}__pre")

        self.log.say("")
        self.log.say(f"  [{row['id']}] {row['why']}")

        t0 = time.time()
        outcome = "?"
        detail = ""
        try:
            if row["method"] == "text":
                res = await drv.bridge_send(str(peer), f"probe {marker}")
                ok = bool((res or {}).get("ok"))
                outcome = "CLAIMED_OK" if ok else "CLAIMED_FAIL"
                detail = str((res or {}).get("code") or "")
                rec["raw"] = res
            else:
                data = self._payload(row["kind"], int(row["size"]))
                if row["mime"] and row["mime"] != "-":
                    mimetypes.add_type(row["mime"],
                                       os.path.splitext(row["name"])[1])
                path = tmp / f"{row['id']}__{row['name']}"
                path.write_bytes(data)
                rec["size_bytes"] = len(data)

                if row["method"] == "api":
                    # A SHORT locate budget: this is a probe, and the default
                    # 45s+25s/MB is what made the previous script take ten
                    # minutes to say nothing.
                    init = await drv.bridge_file_init(
                        str(path), locate_timeout=self.locate)
                    rec["init"] = init
                    if (init or {}).get("ok"):
                        snd = await drv.bridge_file_send(
                            str(peer), caption=f"probe {marker}")
                        rec["send"] = snd
                        outcome = ("CLAIMED_OK" if (snd or {}).get("ok")
                                   else "CLAIMED_FAIL")
                        detail = str((snd or {}).get("code") or "")
                    else:
                        code = str((init or {}).get("code") or "")
                        outcome = ("UPLOAD_VANISHED" if "locate_failed" in code
                                   else "UPLOAD_ERROR")
                        detail = code
                elif row["method"] in ("ui-doc", "ui-photo"):
                    sr = await drv.send_file(
                        str(path), caption=f"probe {marker}", to_saved=True,
                        as_photo=(row["method"] == "ui-photo"))
                    ok = bool(getattr(sr, "ok", False))
                    detail = str(getattr(sr, "detail", "") or "")
                    outcome = "CLAIMED_OK" if ok else "CLAIMED_FAIL"
                    rec["raw"] = {"ok": ok, "detail": detail[:250]}
                elif row["method"] == "direct":
                    outcome, detail = await self._direct(row, path, peer, marker)
                    rec["raw"] = detail if isinstance(detail, dict) else {"d": detail}
                    if isinstance(detail, dict):
                        detail = str(detail.get("code") or "")
        except Exception as exc:  # noqa: BLE001 - one row must never stop the map
            outcome = "EXCEPTION"
            detail = f"{type(exc).__name__}: {exc}"

        rec["elapsed_s"] = round(time.time() - t0, 1)
        rec["claim"] = outcome
        rec["detail"] = str(detail)[:300]
        rec["page_errors"] = await self._errors(page)
        rec["net"] = await self._net(page, f"{row['id']}__net")

        # Deliberately NOT called "sent": the server has not been asked yet.
        self.log.say(f"      claim={outcome}  ({rec['elapsed_s']}s)"
                     + (f"  {rec['detail'][:110]}" if rec["detail"] else ""))
        if rec["page_errors"]:
            uniq = sorted({e.get("msg", "")[:70] for e in rec["page_errors"]})
            self.log.say(f"      page: {'; '.join(uniq[:3])}")

        self.markers[marker] = rec
        self.log.rec(rec)
        return rec

    async def _direct(self, row: dict, path: Path, peer: str,
                      marker: str):
        """Browser-free path. Reports honestly when it is simply unavailable."""
        try:
            from bot import direct_ctx
            if not direct_ctx.has_context(self.account):
                return "UNAVAILABLE", "no browser-free session context captured"
            from direct.sender import DirectSender
        except Exception as exc:  # noqa: BLE001
            return "UNAVAILABLE", f"{type(exc).__name__}: {exc}"
        try:
            snd = DirectSender(self.account)
            up = await asyncio.to_thread(snd.upload_file, str(path))
            if not (up or {}).get("ok", True):
                return "UPLOAD_ERROR", up
            res = await asyncio.to_thread(snd.send_uploaded_file, str(peer),
                                          f"probe {marker}")
            return ("CLAIMED_OK" if (res or {}).get("ok") else "CLAIMED_FAIL"), res
        except Exception as exc:  # noqa: BLE001
            return "EXCEPTION", f"{type(exc).__name__}: {exc}"

    # ---- the only definition of success -------------------------------

    async def verify(self, page) -> dict:
        """Ask the SERVER which markers actually exist.

        This is the part the previous script lacked. "The popup closed" is not
        delivery, and on a restricted account it is actively misleading.
        """
        hist = await page.evaluate(HISTORY_JS, 80)
        self.log.blob("history_final.json", hist)
        found: dict[str, dict] = {}
        for item in (hist.get("items") or []):
            msg = str(item.get("message") or "")
            for marker in self.markers:
                if marker in msg:
                    found[marker] = item
        for marker, rec in self.markers.items():
            item = found.get(marker)
            rec["on_server"] = bool(item)
            rec["server_item"] = item
            rec["verdict"] = "DELIVERED" if item else "NOT_ON_SERVER"
        return hist


# --------------------------------------------------------------------------
# conclusions
# --------------------------------------------------------------------------


def report(log: Log, rows: list[dict], restriction: dict) -> None:
    log.head("WHAT ACTUALLY ARRIVED (server-confirmed)")
    log.say(f"  {'attempt':16s} {'method':10s} {'type':5s} {'claim':16s} verdict")
    for r in rows:
        log.say(f"  {r['id']:16s} {r['method']:10s} {str(r.get('kind')):5s} "
                f"{r.get('claim',''):16s} {r.get('verdict','?')}")

    delivered = [r for r in rows if r.get("verdict") == "DELIVERED"]
    lied = [r for r in rows
            if r.get("claim") == "CLAIMED_OK" and r.get("verdict") != "DELIVERED"]

    if lied:
        log.say("")
        log.say("  ⚠️ These CLAIMED success but never reached the server:")
        for r in lied:
            log.say(f"     {r['id']} ({r['method']})")
        log.say("     That gap is the trap: the UI closes its dialog and the")
        log.say("     message is refused afterwards. Only the server list counts.")

    log.head("WHAT IT MEANS")

    text_row = next((r for r in rows if r["method"] == "text"), None)
    text_ok = bool(text_row and text_row.get("verdict") == "DELIVERED")

    # Restriction first: on a restricted account nothing else is interpretable.
    self_info = (restriction or {}).get("self") or {}
    if self_info.get("restricted") or self_info.get("restriction_reason"):
        log.say("  THE ACCOUNT IS RESTRICTED. Eitaa says so itself:")
        log.say(f"    {json.dumps(self_info, ensure_ascii=False)[:400]}")
        log.say("  Every upload result above is noise until that clears. Test on a")
        log.say("  different account before drawing any apk conclusion.")
        return

    if not delivered:
        log.say("  NOTHING arrived by any method -- not even a plain text message"
                if not text_ok else
                "  Text arrived, but NO file did by any method.")
        if not text_ok:
            log.say("  So this is not about files at all: the account cannot send.")
            log.say("  Check for a restriction/ban, and confirm the session is live")
            log.say("  (panel: 🩺 Session Check).")
        else:
            log.say("  Text works and every upload path fails, so the break is in")
            log.say("  MEDIA specifically, for this account or this server.")
            errs = sorted({e.get("msg", "")[:80] for r in rows
                           for e in (r.get("page_errors") or [])})
            if errs:
                log.say("  Page errors seen during the uploads:")
                for e in errs[:6]:
                    log.say(f"    {e}")
                if any("CONSTRUCTOR" in e.upper() for e in errs):
                    log.say("  A schema/constructor error means the client could not")
                    log.say("  even BUILD the request -- Eitaa never judged the file,")
                    log.say("  so no MIME or filename change can help.")
        return

    log.say(f"  {len(delivered)} of {len(rows)} attempts genuinely arrived.")
    by_method: dict[str, list[dict]] = {}
    for r in delivered:
        by_method.setdefault(r["method"], []).append(r)
    log.say("")
    log.say("  Methods that WORK on this account:")
    for m, rs in by_method.items():
        log.say(f"    {m:10s} -> {', '.join(str(x.get('kind')) for x in rs)}")

    # A text message is not a file. If nothing with an attachment arrived, the
    # break is in MEDIA generally and calling it apk-specific would send the next
    # person hunting MIMEs for a bug that has nothing to do with them.
    files_delivered = [r for r in delivered if r["method"] != "text"]
    if not files_delivered:
        log.say("")
        log.say("  Only TEXT arrived. No file of ANY type made it, so this is not")
        log.say("  an apk rule -- the media path is broken for this account or this")
        log.say("  server, and apk is simply the file you happened to be sending.")
        errs = sorted({e.get("msg", "")[:80] for r in rows
                       for e in (r.get("page_errors") or [])})
        if errs:
            log.say("")
            log.say("  Page errors during the uploads:")
            for e in errs[:6]:
                log.say(f"    {e}")
            if any("CONSTRUCTOR" in e.upper() for e in errs):
                log.say("")
                log.say("  A schema/constructor error means the client could not even")
                log.say("  BUILD the request. Eitaa never saw the file, so no MIME or")
                log.say("  filename change can help -- stop testing those.")
                log.say("  Compare PHASE A's AMM.sendFile signature against what our")
                log.say("  bridge passes: that mismatch is the candidate.")
        if any(r.get("claim") == "CLAIMED_OK" and r["method"].startswith("ui")
               for r in rows):
            log.say("")
            log.say("  Note: a UI attempt reported success and still did not arrive.")
            log.say("  Eitaa accepted it in the interface and refused it afterwards,")
            log.say("  which usually means an account restriction rather than a bug")
            log.say("  in the file path. Re-check PHASE B on THIS account.")
        log.say("")
        log.say(f"  Evidence: {log.dir}")
        return

    apk_ok = [r for r in delivered if r.get("kind") == "apk"]
    if apk_ok:
        log.say("")
        log.say("  ✅ AN APK ARRIVED. Working combinations:")
        for r in apk_ok:
            log.say(f"     {r['id']:16s} method={r['method']:10s} "
                    f"mime={r.get('mime')}")
        api = [r for r in apk_ok if r["method"] == "api"]
        if api:
            log.say("")
            log.say("  The bot's OWN path works, so this is a settings fix:")
            log.say(f"     use MIME {api[0].get('mime')!r} for .apk")
            if api[0].get("mime") == OCTET:
                log.say("     -> turn on 📦 APK send mode and put")
                log.say("        MKWL_APK_OCTET=1 at the TOP of .env")
        else:
            log.say("")
            log.say("  But NOT through the bot's own path -- only through:")
            for m in sorted({r['method'] for r in apk_ok}):
                log.say(f"     {m}")
            log.say("  So the file is acceptable to Eitaa and our API call is not.")
            log.say("  Next: diff the network dumps of a working attempt against a")
            log.say("  failing one -- the difference IS the fix:")
            good = apk_ok[0]["id"]
            bad = next((r["id"] for r in rows
                        if r["method"] == "api" and r.get("kind") == "apk"), "api-apk-octet")
            log.say(f"     net/{good}__net.json   vs   net/{bad}__net.json")
    else:
        log.say("")
        log.say("  ❌ No apk arrived, but OTHER FILES did. That is the useful case:")
        ok_kinds = sorted({str(r.get("kind")) for r in files_delivered})
        log.say(f"     file types that delivered: {', '.join(ok_kinds)}")
        log.say(f"     by method: "
                + ", ".join(f"{m}({len(rs)})" for m, rs in by_method.items()
                            if m != "text"))
        log.say("")
        log.say("     Uploading works, so this IS an apk-specific rule. Push the")
        log.say("     MIME and filename axes -- apk_probe.py has the wide matrix:")
        log.say("       deploy/apk_probe.py --account <acct> --quick")

    log.say("")
    log.say(f"  Evidence: {log.dir}")
    log.say("    attempts.jsonl   every attempt, its claim AND its server verdict")
    log.say("    net/*.json       the page's real traffic per attempt")
    log.say("    history_final.json   what Saved Messages actually contains")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


async def run(args) -> int:
    import tempfile
    from capture.pool import pool as session_pool
    from config import config
    from eitaa.driver import EitaaDriver

    out = Path(config.ARTIFACTS_DIR) / f"apk_explore_{time.strftime('%Y%m%d-%H%M%S')}"
    log = Log(out)
    log.say("APK EXPLORER")
    log.say(f"  account : {args.account}")
    log.say(f"  evidence: {out}")
    log.say(f"  locate budget per probe upload: {args.locate:.0f}s "
            f"(driver default would be ~47s)")
    log.say("  target  : this account's own Saved Messages only")

    rows = matrix(args.deep, args.size_kb)
    log.say(f"  attempts: {len(rows)}")

    tmp = Path(tempfile.mkdtemp(prefix="apk_explore_"))
    ex = Explorer(log, args.account, args.locate)
    restriction: dict = {}

    try:
        init_js = HOOKS_JS if os.path.isfile(HOOKS_JS) else None
        async with session_pool.lease(args.account, headed=config.HEADED_JOBS,
                                      init_script_path=init_js) as session:
            drv = EitaaDriver(session)
            await drv.open()
            if not await drv.is_logged_in():
                log.say("")
                log.say("  ABORT: not logged in. Add/re-login this account first.")
                return 2
            page = drv.page
            await ex._errors(page)         # install the collector

            # ---- phase A: what does this build and this account offer? ----
            log.head("PHASE A — capability surface (no sends)")
            surface = await page.evaluate(SURFACE_JS)
            log.blob("surface.json", surface)
            for k in sorted(surface):
                log.say(f"  {k:34s} {json.dumps(surface[k], ensure_ascii=False)[:150]}")

            log.head("PHASE B — is this account restricted?")
            try:
                restriction = await page.evaluate(RESTRICTION_JS)
            except Exception as exc:  # noqa: BLE001
                restriction = {"error": f"{type(exc).__name__}: {exc}"}
            log.blob("restriction.json", restriction)
            for k in sorted(restriction):
                log.say(f"  {k:20s} {json.dumps(restriction[k], ensure_ascii=False)[:200]}")
            si = (restriction.get("self") or {})
            if si.get("restricted") or si.get("restriction_reason"):
                log.say("")
                log.say("  >>> THIS ACCOUNT IS RESTRICTED. Uploads below would prove")
                log.say("      nothing, so they are skipped.")
                if not args.force:
                    report(log, [], restriction)
                    return 3
                log.say("      --force given, continuing anyway.")

            peer = await drv.self_peer_id()
            if not peer:
                log.say("  ABORT: could not resolve own peer id (stale session?).")
                return 2
            log.say(f"  own peer: {peer}")

            # ---- phase C: the matrix ----
            log.head("PHASE C — method x type")
            for row in rows:
                await ex.attempt(drv, page, str(peer), row, tmp)
                if args.settle:
                    await asyncio.sleep(args.settle)

            # ---- phase D: one server verification for everything ----
            log.head("PHASE D — verifying against the server")
            if args.wait_before_verify:
                log.say(f"  waiting {args.wait_before_verify:.0f}s so anything slow "
                        f"can land...")
                await asyncio.sleep(args.wait_before_verify)
            await ex.verify(page)
            log.say("  done")
    except Exception as exc:  # noqa: BLE001
        log.say("")
        log.say(f"  RUN FAILED: {type(exc).__name__}: {exc}")
        if ex.markers:
            report(log, list(ex.markers.values()), restriction)
        return 1

    report(log, list(ex.markers.values()), restriction)
    return 0 if any(r.get("verdict") == "DELIVERED"
                    and r.get("kind") == "apk" for r in ex.markers.values()) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Map what this account can send, then find a way to send an apk.")
    ap.add_argument("--account", required=True)
    ap.add_argument("--deep", action="store_true",
                    help="also try the photo path and the browser-free path")
    ap.add_argument("--size-kb", type=int, default=8,
                    help="probe payload size in KB (default 8 -- small on purpose)")
    ap.add_argument("--locate", type=float, default=12.0,
                    help="seconds to wait for an upload to appear (default 12; "
                         "the driver default is ~47 and is why the old probe "
                         "took ten minutes)")
    ap.add_argument("--settle", type=float, default=1.0,
                    help="pause between attempts (default 1)")
    ap.add_argument("--wait-before-verify", type=float, default=15.0,
                    help="one wait before the single server check (default 15)")
    ap.add_argument("--force", action="store_true",
                    help="run the uploads even if the account looks restricted")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
