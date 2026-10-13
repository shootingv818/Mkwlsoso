#!/usr/bin/env python3
"""APK send probe — try every way to send an .apk, and capture WHY each one fails.

WHAT THIS IS FOR
----------------
`.apk` sending stopped working and the panel only says:

    upload_failed / locate_failed (upload not found within 174s over 142 checks)

That message is misleading. `eitaa/bridge_file_send.js::__MKWL_fileInit` hands the
file to Eitaa's own upload manager with a unique marker caption, then polls
`messages.getHistory` looking for a message carrying that marker. In the reported
failure NEITHER `sendFile` NOR `getHistory` raised -- both have their own distinct
error codes -- so Eitaa accepted the bytes, answered 142 history calls in 174s
(~1.2s each, so the network was healthy) and never created the message.

It is a SERVER-SIDE REFUSAL, not a timeout. And two numbers fall out of the log:
the budget is `min(420, 45 + 25*MB)`, so 174s means the file was ~5.2 MB.

So this script does two things at once:

  1. TRIES EVERY METHOD. Three genuinely different send paths, crossed with the
     variables Eitaa could possibly be filtering on: the declared MIME, the
     filename, the file's CONTENT (a real apk is a ZIP -- a content sniffer would
     see that), and the size.

  2. CAPTURES EVIDENCE while trying. Every attempt records the page's real
     network traffic (via capture/hooks.js), console errors, Eitaa's OWN internal
     message state after a failure -- which is where the reason lives -- and a
     DELAYED re-check of Saved Messages, because "appeared 30s after we gave up"
     and "never appeared" are different bugs.

Everything lands in `artifacts/apk_probe_<timestamp>/` as a report plus raw
JSONL, so the evidence outlives the terminal.

SAFETY
------
Delivers ONLY to the account's own Saved Messages. Uses a generated dummy file.
Reads project modules; changes no product code and no settings (the APK-mode
toggle is restored on exit). The account is only ever the one you pass in.

    cd ~/Mkwlsoso
    DISPLAY=:99 .venv/bin/python deploy/apk_probe.py --account 989213725238

    # quick pass (small payloads only, ~3-4 min):
    DISPLAY=:99 .venv/bin/python deploy/apk_probe.py --account 989213725238 --quick

    # after finding a candidate, confirm at the real size that failed:
    DISPLAY=:99 .venv/bin/python deploy/apk_probe.py --account 989213725238 \
        --only-mime application/octet-stream --size-mb 5.2
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import mimetypes
import os
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
# evidence log
# --------------------------------------------------------------------------


class Evidence:
    """Console + on-disk record. Nothing here may ever raise into a probe."""

    def __init__(self, outdir: Path) -> None:
        self.dir = outdir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "net").mkdir(exist_ok=True)
        self.cases_path = self.dir / "cases.jsonl"
        self.log_path = self.dir / "probe.log"
        self._log = self.log_path.open("a", encoding="utf-8")

    def say(self, line: str = "") -> None:
        print(line, flush=True)
        try:
            self._log.write(line + "\n")
            self._log.flush()
        except Exception:  # noqa: BLE001
            pass

    def case(self, rec: dict) -> None:
        try:
            with self.cases_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            self.say(f"    [evidence] could not append case: {exc}")

    def blob(self, name: str, payload) -> str | None:
        try:
            p = self.dir / "net" / name
            p.write_text(json.dumps(payload, ensure_ascii=False, default=str,
                                    indent=1), encoding="utf-8")
            return str(p)
        except Exception as exc:  # noqa: BLE001
            self.say(f"    [evidence] could not write {name}: {exc}")
            return None


# --------------------------------------------------------------------------
# dummy payloads
# --------------------------------------------------------------------------


def build_apk_like(size_bytes: int) -> bytes:
    """A dummy file that is genuinely APK-SHAPED.

    A real .apk IS a zip archive containing AndroidManifest.xml and classes.dex.
    If Eitaa sniffs CONTENT rather than trusting the declared MIME, random bytes
    would sail through where a real apk is blocked -- and we would draw exactly
    the wrong conclusion. So the apk-shaped payload is a valid zip with those
    entry names, padded to the requested size.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00dummy-manifest")
        z.writestr("classes.dex", b"dex\n035\x00" + b"\x00" * 64)
        z.writestr("resources.arsc", b"\x02\x00\x0c\x00")
        z.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")
        pad = max(0, size_bytes - buf.tell() - 256)
        if pad:
            z.writestr("assets/pad.bin", b"\x00" * pad)
    return buf.getvalue()


def build_opaque(size_bytes: int) -> bytes:
    """Same size, NOT apk-shaped: random bytes with no zip signature."""
    head = os.urandom(min(4096, size_bytes))
    return head + b"\x00" * max(0, size_bytes - len(head))


# --------------------------------------------------------------------------
# in-page interrogation: where Eitaa records the real reason
# --------------------------------------------------------------------------

#: After a refused upload, Eitaa's own client keeps state about the message it
#: tried to create. This walks several plausible shapes rather than assuming one:
#: Eitaa Web is a fork of Telegram Web K and the internals drift between builds,
#: so a probe that hard-codes one path would silently return nothing. Whatever
#: exists is reported verbatim.
PAGE_STATE_JS = r"""
() => {
  const out = {};
  const safe = (label, fn) => { try { out[label] = fn(); } catch (e) { out[label] = "ERR:" + (e && e.message); } };

  safe("has_appMessagesManager", () => !!window.appMessagesManager);
  safe("fileState", () => {
    const st = window.__MKWL_fileState;
    return st ? { msgId: st.msgId, marker: st.marker, hasDoc: !!st.doc } : null;
  });
  safe("lastErr", () => window.__MKWL_lastErr || null);

  const AMM = window.appMessagesManager;
  if (AMM) {
    // Pending / unsent queues, under the names various builds use.
    for (const key of ["pendingByRandomId", "pendingByMessageId", "pendingAfterMsgs",
                       "sendingMessages", "uploadingMessages"]) {
      safe("AMM." + key, () => {
        const v = AMM[key];
        if (!v) return null;
        const keys = Object.keys(v);
        return { count: keys.length, sample: keys.slice(0, 5) };
      });
    }
    // Any message the client itself marked failed, with whatever error it kept.
    safe("failed_messages", () => {
      const found = [];
      const stores = [AMM.messagesStorageByPeerId, AMM.messagesStorage,
                      AMM.groupedMessagesStorage];
      for (const store of stores) {
        if (!store) continue;
        const outer = Object.values(store);
        for (const inner of outer) {
          const msgs = (inner && typeof inner === "object") ? Object.values(inner) : [];
          for (const m of msgs) {
            if (!m || typeof m !== "object") continue;
            if (m.error || m.failed || m.pFlags && m.pFlags.unsent) {
              found.push({
                id: m.id, error: m.error ? String(m.error).slice(0, 300) : null,
                failed: !!m.failed, unsent: !!(m.pFlags && m.pFlags.unsent),
                message: (m.message || "").slice(0, 80),
                mime: m.media && m.media.document && m.media.document.mime_type,
                size: m.media && m.media.document && m.media.document.size
              });
              if (found.length >= 12) return found;
            }
          }
        }
      }
      return found;
    });
  }

  // Anything the app logged as an error object on window.
  safe("window_errors", () => (window.__MKWL_pageErrors || []).slice(-12));
  return out;
}
"""

#: Read the newest Saved-Messages entries back from the SERVER. Used for the
#: delayed re-check: a message that turns up after the poll gave up means the
#: upload was slow, which is a completely different fix from a refusal.
SAVED_HISTORY_JS = r"""
async (limit) => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
  try {
    const h = await AM.invokeApi("messages.getHistory", {
      peer: { _: "inputPeerSelf" }, offset_id: 0, offset_date: 0, add_offset: 0,
      limit: limit || 20, max_id: 0, min_id: 0, hash: 0 });
    const msgs = (h && h.messages) || [];
    return { ok: true, items: msgs.map(m => ({
      id: m.id,
      message: (m.message || "").slice(0, 90),
      hasDoc: !!(m.media && m.media.document),
      mime: m.media && m.media.document && m.media.document.mime_type,
      size: m.media && m.media.document && m.media.document.size,
      name: (() => { try {
        const a = ((m.media||{}).document||{}).attributes || [];
        for (const x of a) if (x.file_name) return x.file_name;
      } catch (e) {} return null; })()
    })) };
  } catch (e) { return { ok: false, code: String(e && e.message || e) }; }
}
"""

PAGE_ERROR_COLLECTOR_JS = r"""
() => {
  if (window.__MKWL_pageErrors) return true;
  window.__MKWL_pageErrors = [];
  const push = (o) => { try { if (window.__MKWL_pageErrors.length < 200) window.__MKWL_pageErrors.push(o); } catch (e) {} };
  window.addEventListener("error", (e) => push({ t: "error", msg: String(e.message).slice(0,300) }));
  window.addEventListener("unhandledrejection", (e) => push({
    t: "rejection", msg: String((e.reason && (e.reason.message || e.reason)) || "").slice(0,300) }));
  return true;
}
"""


# --------------------------------------------------------------------------
# the case matrix
# --------------------------------------------------------------------------


def build_cases(quick: bool, only_mime: str | None, size_mb: float) -> list[dict]:
    """Every combination worth trying, cheapest first.

    Deliberately ordered so the answer arrives early: the control proves uploading
    works at all, then the two cases that reproduce/repair the known bug, then the
    variables that would only matter if those two disagree with the record.

    Small payloads for the matrix. Finding the boundary with 64 KB uploads and
    THEN confirming the winner at the real size is far faster than running the
    whole grid at 5 MB -- and the size axis is itself one of the cases.
    """
    SMALL = int(0.064 * 1024 * 1024)
    MED = int(1.0 * 1024 * 1024)
    REAL = int(size_mb * 1024 * 1024)

    cases: list[dict] = [
        # --- controls: does uploading work at all right now? ---
        dict(id="control-zip", method="bridge", name="control.zip",
             mime="application/zip", shape="apk", size=SMALL,
             why="CONTROL: a plain zip must send, or nothing below means anything"),
        dict(id="control-txt", method="bridge", name="control.txt",
             mime="text/plain", shape="opaque", size=SMALL,
             why="CONTROL: smallest possible document"),

        # --- the known bug and the recorded fix ---
        dict(id="apk-real-mime", method="bridge", name="app.apk",
             mime=BLOCKED_APK_MIME, shape="apk", size=SMALL,
             why="reproduces the documented block (real apk MIME)"),
        dict(id="apk-octet", method="bridge", name="app.apk",
             mime=OCTET, shape="apk", size=SMALL,
             why="THE RECORDED FIX: apk name + generic binary"),

        # --- is it the MIME? alternative declared types ---
        dict(id="apk-binary", method="bridge", name="app.apk",
             mime="application/binary", shape="apk", size=SMALL,
             why="alternative smuggle MIME"),
        dict(id="apk-vnd-short", method="bridge", name="app.apk",
             mime="application/vnd.android.package", shape="apk", size=SMALL,
             why="near-miss of the blocked type: is the match exact or a prefix?"),
        dict(id="apk-zip-mime", method="bridge", name="app.apk",
             mime="application/zip", shape="apk", size=SMALL,
             why="apk name, honest zip MIME (an apk IS a zip)"),
        dict(id="apk-stream-x", method="bridge", name="app.apk",
             mime="application/x-binary", shape="apk", size=SMALL,
             why="another generic binary spelling"),
        dict(id="apk-empty-mime", method="bridge", name="app.apk",
             mime="", shape="apk", size=SMALL,
             why="no MIME at all: does Eitaa then sniff the content?"),

        # --- is it the NAME? ---
        dict(id="name-upper", method="bridge", name="APP.APK",
             mime=OCTET, shape="apk", size=SMALL,
             why="uppercase extension: is the name check case-sensitive?"),
        dict(id="name-double", method="bridge", name="app.apk.bin",
             mime=OCTET, shape="apk", size=SMALL,
             why="apk in the middle of the name"),
        dict(id="name-neutral", method="bridge", name="app.bin",
             mime=OCTET, shape="apk", size=SMALL,
             why="apk CONTENT under a neutral name: is content sniffed?"),
        dict(id="name-none", method="bridge", name="appfile",
             mime=OCTET, shape="apk", size=SMALL,
             why="no extension at all"),

        # --- is it the CONTENT? ---
        dict(id="opaque-apkname", method="bridge", name="app.apk",
             mime=OCTET, shape="opaque", size=SMALL,
             why="apk NAME but NOT apk bytes: separates name from content"),

        # --- is it the SIZE? ---
        dict(id="size-1mb", method="bridge", name="app.apk",
             mime=OCTET, shape="apk", size=MED,
             why="same as the fix, 1 MB"),
        dict(id="size-real", method="bridge", name="app.apk",
             mime=OCTET, shape="apk", size=REAL,
             why=f"same as the fix, {size_mb} MB -- the size that actually failed"),

        # --- a different METHOD entirely: the real UI, like a human ---
        dict(id="ui-attach-apk", method="ui", name="app.apk",
             mime=OCTET, shape="apk", size=SMALL,
             why="UI ATTACH PATH: drives the real file chooser, not the API"),
        dict(id="ui-attach-zip", method="ui", name="control.zip",
             mime="application/zip", shape="apk", size=SMALL,
             why="UI attach control"),
    ]

    if quick:
        keep = {"control-zip", "apk-real-mime", "apk-octet", "apk-binary",
                "apk-zip-mime", "name-neutral", "opaque-apkname", "ui-attach-apk"}
        cases = [c for c in cases if c["id"] in keep]
    if only_mime:
        cases = [c for c in cases if c["mime"] == only_mime or c["id"].startswith("control")]
    return cases


# --------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------


class Probe:
    def __init__(self, ev: Evidence, account: str, settle: float,
                 delayed: float) -> None:
        self.ev = ev
        self.account = account
        self.settle = settle
        self.delayed = delayed
        self.results: list[dict] = []

    # ---- helpers -------------------------------------------------------

    async def _drain_net(self, page, tag: str) -> dict:
        """Take everything capture/hooks.js buffered and keep the media traffic."""
        summary = {"total": 0, "media": [], "file": None}
        try:
            from capture import deep
        except Exception:  # noqa: BLE001
            deep = None
        records: list[dict] = []
        if deep is not None:
            await deep.pull_hooks(page, lambda e: records.append(e))
        else:
            try:
                raw = await page.evaluate(
                    "() => (window.__MKWL_dump ? window.__MKWL_dump() : [])")
                records = list(raw or [])
            except Exception:  # noqa: BLE001
                records = []
        summary["total"] = len(records)
        interesting = []
        for r in records:
            url = str(r.get("url") or "")
            kind = str(r.get("k") or r.get("kind") or "")
            if any(w in url.lower() for w in ("upload", "media", "file", "eitaa")) \
                    or kind.startswith("worker"):
                interesting.append({
                    "kind": kind, "url": url[:160],
                    "status": r.get("status"),
                    "reqSize": r.get("reqSize"), "respSize": r.get("respSize"),
                })
        summary["media"] = interesting[:60]
        if records:
            summary["file"] = self.ev.blob(f"{tag}.json", records)
        return summary

    async def _page_state(self, page) -> dict:
        try:
            return await page.evaluate(PAGE_STATE_JS)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    async def _saved_history(self, page, limit: int = 20) -> dict:
        try:
            return await page.evaluate(SAVED_HISTORY_JS, limit)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"{type(exc).__name__}: {exc}"}

    def _classify(self, res: dict | None, send: dict | None) -> tuple[str, str]:
        """Turn the raw results into ONE state plus the reason.

        The distinction that matters, because the panel shows both as
        'upload_failed':

          REFUSED_SILENTLY  locate_failed -- accepted for upload, then no message
                            was ever created. A server-side refusal.
          UPLOAD_ERROR      sendFile/getHistory threw -- the page or the network
                            broke before Eitaa ever judged the file.
        """
        res = res or {}
        if not res.get("ok"):
            code = str(res.get("code") or res.get("error") or "unknown")
            if "locate_failed" in code:
                return "REFUSED_SILENTLY", code
            return "UPLOAD_ERROR", code
        send = send or {}
        if send.get("ok"):
            return "SENT", ""
        return "SEND_REJECTED", str(send.get("code") or send.get("error") or "unknown")

    # ---- one case ------------------------------------------------------

    async def run_case(self, drv, page, peer: str, case: dict,
                       tmpdir: Path) -> dict:
        from direct import apk_mode

        name, mime, shape = case["name"], case["mime"], case["shape"]
        size = int(case["size"])
        payload = (build_apk_like(size) if shape == "apk" else build_opaque(size))
        path = tmpdir / f"{case['id']}__{name}"
        path.write_bytes(payload)

        # Steer the wire MIME through the product's own code path: driver calls
        # mimetypes.guess_type(), so registering the type here is enough. APK mode
        # is forced OFF so effective_mime() cannot overwrite the value under test;
        # the recorded fix is represented by the explicit octet case instead.
        apk_mode.set_env(False)
        ext = os.path.splitext(name)[1]
        if ext and mime:
            mimetypes.add_type(mime, ext)
        wire_expect = mime or OCTET

        self.ev.say("")
        self.ev.say(f"  [{case['id']}]  {case['why']}")
        self.ev.say(f"      method={case['method']}  name={name}  "
                    f"mime={wire_expect!r}  shape={shape}  "
                    f"size={len(payload):,}B")

        # Clear the hook buffer so this case's traffic stands alone.
        await self._drain_net(page, f"{case['id']}__pre")

        rec: dict = {"case": case["id"], "why": case["why"],
                     "method": case["method"], "name": name,
                     "mime_requested": mime, "shape": shape,
                     "size_bytes": len(payload), "ts": time.time()}
        t0 = time.time()
        init_res: dict | None = None
        send_res: dict | None = None

        try:
            if case["method"] == "bridge":
                init_res = await drv.bridge_file_init(str(path))
                rec["init"] = init_res
                if (init_res or {}).get("ok"):
                    send_res = await drv.bridge_file_send(
                        str(peer), caption=f"apk probe {case['id']} - ignore")
                    rec["send"] = send_res
            elif case["method"] == "ui":
                # A different mechanism entirely: the paperclip + real file
                # chooser, i.e. exactly what a human does. If the API path is
                # refused and this one is not, the filter is on the API call
                # rather than on the file.
                sr = await drv.send_file(str(path), caption=f"apk probe {case['id']}",
                                         to_saved=True)
                ok = bool(getattr(sr, "ok", False))
                detail = str(getattr(sr, "detail", "") or "")
                init_res = {"ok": ok, "code": detail}
                send_res = {"ok": ok, "code": detail}
                rec["ui_result"] = {"ok": ok, "detail": detail[:300]}
            else:
                init_res = {"ok": False, "code": f"unknown method {case['method']}"}
        except Exception as exc:  # noqa: BLE001 - a case must never abort the run
            init_res = {"ok": False, "code": f"{type(exc).__name__}: {exc}"}
            rec["exception"] = f"{type(exc).__name__}: {exc}"

        rec["elapsed_s"] = round(time.time() - t0, 1)
        state, reason = self._classify(init_res, send_res)
        rec["state"] = state
        rec["reason"] = reason

        # ---- evidence, every time, not only on failure ----
        rec["net"] = await self._drain_net(page, f"{case['id']}__net")
        rec["page_state"] = await self._page_state(page)

        mark = {"SENT": "SENT ✅", "REFUSED_SILENTLY": "REFUSED (silent) ❌",
                "SEND_REJECTED": "SEND REJECTED ❌",
                "UPLOAD_ERROR": "UPLOAD ERROR ⚠️"}.get(state, state)
        self.ev.say(f"      -> {mark}  ({rec['elapsed_s']}s)"
                    + (f"  {reason[:120]}" if reason else ""))
        if rec["net"]["total"]:
            self.ev.say(f"         net: {rec['net']['total']} records, "
                        f"{len(rec['net']['media'])} media/worker")

        # Eitaa's own view of the failure is the most valuable single artefact.
        if state != "SENT":
            failed = (rec["page_state"] or {}).get("failed_messages")
            if failed:
                self.ev.say(f"         page reports failed/unsent messages: "
                            f"{json.dumps(failed, ensure_ascii=False)[:400]}")
            errs = (rec["page_state"] or {}).get("window_errors")
            if errs:
                self.ev.say(f"         page errors: "
                            f"{json.dumps(errs, ensure_ascii=False)[:300]}")

        # ---- delayed re-check: slow is not the same as refused ----
        if state == "REFUSED_SILENTLY" and self.delayed > 0:
            self.ev.say(f"         waiting {self.delayed:.0f}s to see if it turns "
                        f"up late...")
            await asyncio.sleep(self.delayed)
            hist = await self._saved_history(page, 20)
            rec["delayed_history"] = hist
            late = None
            for item in (hist.get("items") or []):
                if item.get("hasDoc") and item.get("size") == len(payload):
                    late = item
                    break
            rec["appeared_late"] = bool(late)
            if late:
                self.ev.say(f"         IT APPEARED LATE: {json.dumps(late, ensure_ascii=False)}")
                self.ev.say("         => this is a TIMEOUT problem, not a refusal")
            else:
                self.ev.say("         still absent => a real refusal, not slowness")

        if self.settle > 0:
            await asyncio.sleep(self.settle)
        self.ev.case(rec)
        self.results.append(rec)
        return rec


# --------------------------------------------------------------------------
# conclusions
# --------------------------------------------------------------------------


def conclude(ev: Evidence, results: list[dict]) -> None:
    ev.say("")
    ev.say("=" * 74)
    ev.say("RESULTS")
    ev.say("=" * 74)
    ev.say(f"  {'case':18s} {'name':14s} {'mime':40s} state")
    for r in results:
        ev.say(f"  {r['case']:18s} {r['name']:14s} "
               f"{str(r['mime_requested'])[:40]:40s} {r['state']}")

    by = {r["case"]: r for r in results}

    def st(cid: str) -> str | None:
        return (by.get(cid) or {}).get("state")

    ev.say("")
    ev.say("=" * 74)
    ev.say("WHAT THE EVIDENCE SAYS")
    ev.say("=" * 74)

    controls = [c for c in ("control-zip", "control-txt") if c in by]
    if controls and all(st(c) != "SENT" for c in controls):
        ev.say("  Uploading is broken for EVERY file type, apk or not.")
        ev.say("  This is not the MIME filter. Look at, in order:")
        ev.say("    - the account: is it restricted for media? (panel: Session Check)")
        ev.say("    - the media host fateme.eitaa.com: reachable from this server?")
        ev.say("    - disk space and the browser profile")
        if any((by[c].get("page_state") or {}).get("failed_messages")
               for c in controls):
            ev.say("  The page reported failed/unsent messages -- read them in")
            ev.say("  cases.jsonl under page_state.failed_messages: that is Eitaa's")
            ev.say("  own reason, not our guess.")
        return

    if controls:
        ev.say("  Controls sent, so uploading itself works.")

    # A case only counts as "a real apk can be sent" when the NAME was .apk AND
    # the BYTES were apk-shaped. A success with opaque bytes under an .apk name
    # proves something about content sniffing, but recommending its MIME would be
    # advice that cannot work on a real apk -- so those are reported as evidence
    # further down, never as the fix.
    real_apk_sent = [r for r in results
                     if r["state"] == "SENT"
                     and r["name"].lower().endswith(".apk")
                     and r["shape"] == "apk"]
    via_bridge = [r for r in real_apk_sent if r["method"] == "bridge"]
    via_ui = [r for r in real_apk_sent if r["method"] == "ui"]

    if via_bridge:
        ev.say("")
        ev.say("  A REAL apk (apk name + apk bytes) DELIVERED on the bot's own")
        ev.say("  send path with these MIMEs:")
        for r in via_bridge:
            ev.say(f"    {r['case']:18s} mime={r['mime_requested']!r} "
                   f"size={r['size_bytes']:,}")
        best = via_bridge[0]
        ev.say("")
        if best["mime_requested"] == OCTET:
            ev.say("  => The recorded fix still works. Nothing to change in code:")
            ev.say("     turn on 📦 APK send mode, and put MKWL_APK_OCTET=1 at the")
            ev.say("     TOP of .env so it survives a restart (the loader keeps the")
            ev.say("     FIRST occurrence of a key).")
        else:
            ev.say(f"  => Use {best['mime_requested']!r}. This is NOT what the")
            ev.say("     product sends today: change GENERIC_BINARY in")
            ev.say("     direct/apk_mode.py to this value.")
        sizes = sorted(r["size_bytes"] for r in via_bridge)
        if sizes and sizes[-1] < 1_000_000:
            ev.say("")
            ev.say("     Only SMALL payloads were proven. Confirm at the real size")
            ev.say("     before trusting it:")
            ev.say(f"       --only-mime {best['mime_requested']} --size-mb 5.2")
    elif via_ui:
        ev.say("")
        ev.say("  A real apk delivered ONLY through the UI ATTACH path, not the")
        ev.say("  bot's API path:")
        for r in via_ui:
            ev.say(f"    {r['case']:18s} mime={r['mime_requested']!r}")
        ev.say("")
        ev.say("  => The filter is on the API call, not on the file. The bytes and")
        ev.say("     the name are acceptable to Eitaa; the way the bot submits them")
        ev.say("     is not. Options, in order of cost:")
        ev.say("       1. diff the two requests: net/ui-attach-apk__net.json against")
        ev.say("          net/apk-octet__net.json -- the difference IS the answer")
        ev.say("       2. port file sending to the UI path (works, but far slower")
        ev.say("          per recipient and needs the browser for every send)")
    else:
        ev.say("")
        ev.say("  NO real apk got through by any method. What that rules in and out:")
        name_neutral_ok = st("name-neutral") == "SENT"
        opaque_ok = st("opaque-apkname") == "SENT"
        if name_neutral_ok and opaque_ok:
            ev.say("    - apk bytes passed under a NEUTRAL name, and non-apk bytes")
            ev.say("      passed under an .apk name -- but the two together did not.")
            ev.say("      So NEITHER alone is blocked: Eitaa is refusing the")
            ev.say("      COMBINATION of an apk name and apk content. Defeating that")
            ev.say("      means changing one of them, and both change what the")
            ev.say("      recipient gets. Decide deliberately, do not just flip a")
            ev.say("      MIME and hope.")
        elif name_neutral_ok:
            ev.say("    - apk BYTES under a neutral name DID send, so the content is")
            ev.say("      acceptable and Eitaa is filtering the .apk FILENAME.")
            ev.say("      Different fix from the MIME one: the name in")
            ev.say("      documentAttributeFilename would have to change, which")
            ev.say("      changes what the recipient receives.")
        elif opaque_ok:
            ev.say("    - NON-apk bytes under an .apk name sent, but apk-shaped bytes")
            ev.say("      did not. Eitaa is sniffing the CONTENT (the zip signature /")
            ev.say("      AndroidManifest.xml). No MIME or filename trick defeats")
            ev.say("      content inspection -- stop looking for one.")
        else:
            ev.say("    - Neither a neutral name nor non-apk bytes helped, so the")
            ev.say("      block is not a simple name or content check.")
        if st("apk-empty-mime") == "SENT":
            ev.say("    - Sending NO MIME at all worked. Eitaa fills it in itself and")
            ev.say("      evidently lands on something acceptable.")
        if st("apk-real-mime") == "SENT":
            ev.say("    - The real apk MIME was ACCEPTED, so the documented block is")
            ev.say("      gone and something else is failing. Re-read the states")
            ev.say("      above as a fresh problem, not the old one.")
        refused = [r for r in results if r["state"] == "REFUSED_SILENTLY"]
        late = [r for r in refused if r.get("appeared_late")]
        if late:
            ev.say(f"    - {len(late)} upload(s) APPEARED AFTER the poll gave up.")
            ev.say("      Those are timeouts, not refusals: raise the locate budget")
            ev.say("      in bridge_file_init (currently 45s + 25s/MB) before")
            ev.say("      changing anything about MIME.")
        elif refused:
            ev.say(f"    - {len(refused)} upload(s) were accepted and then never")
            ev.say("      materialised, with no late arrival. Server-side refusal.")
        ev.say("")
        ev.say("  NEXT EVIDENCE TO GATHER (in order of value):")
        ev.say("    1. page_state.failed_messages in cases.jsonl -- Eitaa's own")
        ev.say("       reason for the refusal, if its client kept one.")
        ev.say("    2. net/*.json for a refused case: the upload.saveFilePart and")
        ev.say("       messages.sendMedia exchanges, including the reply body.")
        ev.say("    3. A real competitor send captured with")
        ev.say("       deploy/eitaa_deep_probe.py, to diff against ours.")

    ev.say("")
    ev.say(f"  Evidence bundle: {ev.dir}")
    ev.say(f"    cases.jsonl   every attempt with its full raw results")
    ev.say(f"    net/*.json    the page's real network records per attempt")
    ev.say(f"    probe.log     this transcript")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


async def run(args) -> int:
    import tempfile
    from capture.pool import pool as session_pool
    from config import config
    from direct import apk_mode
    from eitaa.driver import EitaaDriver

    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = Path(config.ARTIFACTS_DIR) / f"apk_probe_{stamp}"
    ev = Evidence(outdir)

    ev.say("APK SEND PROBE")
    ev.say(f"  account : {args.account}")
    ev.say(f"  repo    : {_ROOT}")
    ev.say(f"  evidence: {outdir}")
    ev.say(f"  target  : this account's own Saved Messages ONLY")
    ev.say("")
    ev.say(f"  this server maps .apk -> "
           f"{mimetypes.guess_type('x.apk')[0]!r}")
    ev.say(f"  APK send mode currently: {apk_mode.enabled()}")

    cases = build_cases(args.quick, args.only_mime, args.size_mb)
    ev.say(f"  cases   : {len(cases)}")

    saved_flag = os.environ.get(apk_mode.APK_OCTET_ENV)
    tmpdir = Path(tempfile.mkdtemp(prefix="apk_probe_"))
    probe = Probe(ev, args.account, args.settle, args.delayed_check)

    try:
        # hooks.js is injected as an init script so it wraps fetch/XHR/worker
        # BEFORE Eitaa's own code runs -- that is the only way to see the upload
        # traffic rather than guess at it.
        init_script = HOOKS_JS if os.path.isfile(HOOKS_JS) else None
        if init_script is None:
            ev.say("  WARNING: capture/hooks.js not found -- no network evidence")
        async with session_pool.lease(args.account, headed=config.HEADED_JOBS,
                                      init_script_path=init_script) as session:
            drv = EitaaDriver(session)
            await drv.open()
            if not await drv.is_logged_in():
                ev.say("")
                ev.say("  ABORT: this account is not logged in.")
                ev.say("  Add or re-login it in the panel, then re-run.")
                return 2
            page = drv.page
            try:
                await page.evaluate(PAGE_ERROR_COLLECTOR_JS)
            except Exception as exc:  # noqa: BLE001
                ev.say(f"  (page error collector unavailable: {exc})")

            peer = None
            try:
                peer = await drv.self_peer_id()
            except Exception as exc:  # noqa: BLE001
                ev.say(f"  could not resolve own peer: {exc}")
            if not peer:
                ev.say("  ABORT: could not resolve this account's own peer id.")
                ev.say("  The session is probably stale: run Session Check.")
                return 2
            ev.say(f"  own peer: {peer}")

            before = await probe._saved_history(page, 5)
            ev.blob("saved_before.json", before)

            for case in cases:
                await probe.run_case(drv, page, str(peer), case, tmpdir)

            after = await probe._saved_history(page, 30)
            ev.blob("saved_after.json", after)
    except Exception as exc:  # noqa: BLE001
        ev.say("")
        ev.say(f"  RUN FAILED: {type(exc).__name__}: {exc}")
        ev.say("  Check DISPLAY=:99, that xvfb is running, and the account session.")
        if probe.results:
            conclude(ev, probe.results)
        return 1
    finally:
        if saved_flag is None:
            os.environ.pop(apk_mode.APK_OCTET_ENV, None)
        else:
            os.environ[apk_mode.APK_OCTET_ENV] = saved_flag

    conclude(ev, probe.results)
    return 0 if any(r["state"] == "SENT" for r in probe.results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Try every way to send an .apk and capture why each fails.")
    ap.add_argument("--account", required=True, help="e.g. 989213725238")
    ap.add_argument("--size-mb", type=float, default=5.2,
                    help="the real-size case (default 5.2, derived from the "
                         "174s budget in the reported failure)")
    ap.add_argument("--quick", action="store_true",
                    help="only the 8 highest-value cases")
    ap.add_argument("--only-mime", default=None,
                    help="run just this MIME (plus the controls) -- for "
                         "confirming a candidate at full size")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds between cases (default 3)")
    ap.add_argument("--delayed-check", type=float, default=30.0,
                    help="after a silent refusal, wait this long and look "
                         "again; 0 disables (default 30)")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
