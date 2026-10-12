#!/usr/bin/env python3
"""Watch what another bot sends on this Eitaa account, and dissect every file.

The plan
--------
Another Telegram bot drives the SAME Eitaa account and sends apks (as zips) to
contacts. We want to know EXACTLY how it does it - the trick that gets an apk
past Eitaa's filter - so we can copy it byte for byte.

This opens the account in our own browser, snapshots the chats/Saved Messages
NOW, then watches. Whenever a NEW document appears anywhere (sent by the other
bot, or received), it:

  * records the full document metadata Eitaa stores: file name, mime_type, size,
    the documentAttribute list, dc_id, every field
  * DOWNLOADS the actual bytes via upload.getFile
  * dissects them: is it a zip? what are the ENTRY NAMES inside (the thing Eitaa
    filters on)? is it encrypted? what is the inner structure? first bytes in
    hex? if an entry is itself an apk/zip, it recurses
  * saves the raw file to artifacts/ so you have the real artifact to inspect

So when the other bot sends its zip, we see precisely what it named the file,
what it named the entry INSIDE, whether it is encrypted, and how it is built -
"every single hair out of place" is captured.

Two ways to run it:
  --watch N     poll for N seconds; you trigger the other bot meanwhile, and any
                new document that lands is dissected live (default)
  --scan-only   just dissect the most recent documents already in the account
                (use this if the other bot ALREADY sent its file)

It reads only; it sends nothing. Two browsers on one account is fine (Eitaa is
multi-session). Own account only.

    systemctl stop mkwlsoso-bot   # optional; not required
    cd ~/Mkwlsoso && .venv/bin/python deploy/spy_other_bot.py --account 989939094655 --watch 180
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import time
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"documents": [], "verdict": []}
_T0 = time.time()
_OUTDIR = None


def banner(t): print("\n" + "=" * 78 + f"\n  {t}\n" + "=" * 78, flush=True)
def log(m=""): print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)
def ok(m): log(f"OK    {m}")
def bad(m): log(f"FAIL  {m}")
def info(m): log(f"      {m}")
def hit(m): log(f"####  {m}")


def dump(label, obj, indent="        "):
    info(label)
    for ln in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines()[:50]:
        print(indent + ln, flush=True)


async def ev(page, js, arg=None, timeout=90):
    try:
        if arg is not None:
            return (await asyncio.wait_for(page.evaluate(js, arg), timeout=timeout), None)
        return (await asyncio.wait_for(page.evaluate(js), timeout=timeout), None)
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# in-page: list recent documents across dialogs, and download one by bytes
# ---------------------------------------------------------------------------

# Walk recent dialogs and collect every message that carries a document, with
# full metadata. This is what the other bot's sends look like on the wire.
LIST_DOCS_JS = r"""
async (limitPerChat) => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
  const out = { docs: [] };
  // dialogs first
  let peers = [{_:"inputPeerSelf"}];
  try {
    const d = await AM.invokeApi("messages.getDialogs", {
      offset_date:0, offset_id:0, offset_peer:{_:"inputPeerEmpty"},
      limit:20, hash:0, exclude_pinned:false, folder_id:0 });
    // build inputPeers from the dialogs' chats/users
    const uById = {}; for (const u of (d.users||[])) uById[u.id]=u;
    const cById = {}; for (const c of (d.chats||[])) cById[c.id]=c;
    for (const dl of (d.dialogs||[])) {
      const p = dl.peer;
      try {
        if (p._==="peerUser" && uById[p.user_id]) {
          const u=uById[p.user_id];
          peers.push({_:"inputPeerUser", user_id:u.id, access_hash:u.access_hash});
        } else if (p._==="peerChannel" && cById[p.channel_id]) {
          const c=cById[p.channel_id];
          peers.push({_:"inputPeerChannel", channel_id:c.id, access_hash:c.access_hash});
        } else if (p._==="peerChat") {
          peers.push({_:"inputPeerChat", chat_id:(cById[p.chat_id]||{}).id||p.chat_id});
        }
      } catch(e){}
    }
  } catch(e){ out.dialogs_err = String(e); }

  for (const peer of peers.slice(0, 25)) {
    try {
      const h = await AM.invokeApi("messages.getHistory", {
        peer, offset_id:0, offset_date:0, add_offset:0,
        limit: limitPerChat||8, max_id:0, min_id:0, hash:0 });
      for (const m of (h.messages||[])) {
        const d = m.media && m.media.document;
        if (!d) continue;
        let fname=null; const attrs=[];
        for (const at of (d.attributes||[])) {
          attrs.push(at._);
          if (at._==="documentAttributeFilename") fname=at.file_name;
        }
        out.docs.push({
          msg_id:m.id, date:m.date, out:!!m.pFlags && !!m.pFlags.out,
          peer_kind:peer._,
          file_name:fname, mime:d.mime_type, size:String(d.size),
          dc_id:d.dc_id, doc_id:String(d.id),
          access_hash:String(d.access_hash),
          file_reference_hex:(function(fr){ try{
            if(!fr) return null; let h=""; const b=new Uint8Array(fr);
            for(let i=0;i<b.length;i++) h+=b[i].toString(16).padStart(2,"0"); return h;
          }catch(e){return null;} })(d.file_reference),
          attributes: attrs,
          caption: (m.message||"").slice(0,80)
        });
      }
    } catch(e){}
  }
  // newest first
  out.docs.sort((a,b)=>(b.date||0)-(a.date||0));
  return {ok:true, docs: out.docs.slice(0, 40)};
}
"""

# Download a document's bytes by (id, access_hash, file_reference, dc_id) via
# upload.getFile, returned base64. Capped size for safety.
DOWNLOAD_JS = r"""
async (a) => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
  function hx2u(h){ const u=new Uint8Array(h.length/2); for(let i=0;i<u.length;i++) u[i]=parseInt(h.substr(i*2,2),16); return u; }
  const loc = {
    _:"inputDocumentFileLocation",
    id: a.doc_id, access_hash: a.access_hash,
    file_reference: a.file_reference_hex ? hx2u(a.file_reference_hex) : new Uint8Array(0),
    thumb_size: ""
  };
  const cap = a.cap || (1024*1024);  // 1 MB default; enough to see structure
  let offset = 0; const chunks = [];
  try {
    while (offset < cap) {
      const part = await AM.invokeApi("upload.getFile", {
        location: loc, offset: offset, limit: 524288, precise:false, cdn_supported:false
      }, { dcId: a.dc_id });
      const bytes = part && part.bytes;
      if (!bytes || !bytes.length) break;
      const b = new Uint8Array(bytes);
      let s=""; for(let i=0;i<b.length;i++) s+=String.fromCharCode(b[i]);
      chunks.push(btoa(s));
      offset += b.length;
      if (b.length < 524288) break;
    }
    return {ok:true, b64parts: chunks, bytes: offset};
  } catch(e){ return {ok:false, code:String(e), bytes: offset}; }
}
"""


def dissect(name: str, data: bytes, depth: int = 0) -> dict:
    """Pull apart a file: zip? entry names? encrypted? recurse into inner apks."""
    out = {"size": len(data), "head_hex": data[:16].hex()}
    # zip magic PK\x03\x04 or PK\x05\x06 (empty) or PK\x07\x08
    is_zip = data[:2] == b"PK"
    out["looks_zip"] = is_zip
    if is_zip:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            entries = []
            for zi in z.infolist():
                enc = bool(zi.flag_bits & 0x1)
                entries.append({"name": zi.filename, "size": zi.file_size,
                                "compress": zi.compress_type, "encrypted": enc})
            out["zip_entries"] = entries
            out["any_encrypted"] = any(e["encrypted"] for e in entries)
            out["entry_names"] = [e["name"] for e in entries]
            # does an entry end in .apk? (what Eitaa filters on)
            out["has_apk_named_entry"] = any(
                e["name"].lower().endswith(".apk") for e in entries)
            # recurse one level into a nested zip/apk entry
            if depth < 2:
                for zi in z.infolist():
                    low = zi.filename.lower()
                    if (low.endswith(".apk") or low.endswith(".zip")) and not (zi.flag_bits & 0x1):
                        try:
                            inner = z.read(zi.filename)
                            out.setdefault("nested", {})[zi.filename] = dissect(
                                zi.filename, inner, depth + 1)
                        except Exception as exc:  # noqa: BLE001
                            out.setdefault("nested", {})[zi.filename] = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            out["zip_error"] = str(exc)
    else:
        # gzip? 7z? apk(=zip)? identify by magic
        magic = {
            b"\x1f\x8b": "gzip", b"7z\xbc\xaf": "7z", b"Rar!": "rar",
            b"\x00\x00\x00": "possibly tar/other",
        }
        for m, label in magic.items():
            if data[:len(m)] == m:
                out["magic_guess"] = label
                break
    return out


# ==========================================================================
# runner
# ==========================================================================

READBACK_READY = (
    "() => { if (window.__MKWL_api) return true; return !!(window.apiManager "
    "&& window.apiManager.invokeApi); }")


async def download_and_dissect(page, doc: dict, cap_bytes: int) -> dict:
    """Download one document's bytes and pull it apart."""
    arg = {"doc_id": doc["doc_id"], "access_hash": doc["access_hash"],
           "file_reference_hex": doc.get("file_reference_hex"),
           "dc_id": doc.get("dc_id"), "cap": cap_bytes}
    res, err = await ev(page, DOWNLOAD_JS, arg, timeout=120)
    if err or not res or not res.get("ok"):
        return {"download_error": (res or {}).get("code") or err}
    try:
        raw = b"".join(base64.b64decode(p) for p in res.get("b64parts", []))
    except Exception as exc:  # noqa: BLE001
        return {"decode_error": str(exc)}
    # save the real artifact
    saved = None
    try:
        fn = (doc.get("file_name") or f"doc_{doc['doc_id']}").replace("/", "_")
        path = _OUTDIR / fn
        path.write_bytes(raw)
        saved = str(path)
    except Exception:  # noqa: BLE001
        pass
    anal = dissect(doc.get("file_name") or "", raw)
    anal["downloaded_bytes"] = len(raw)
    anal["saved_to"] = saved
    return anal


async def run(args) -> int:
    global _OUTDIR
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    _OUTDIR = config.ARTIFACTS_DIR / f"spy_{time.strftime('%Y%m%d-%H%M%S')}"
    _OUTDIR.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver

    banner("STEP 1  -  open the account (read-only spy; sends nothing)")
    session = await BrowserSession(args.account, headed=False, light_assets=None).start()
    page = session.page
    try:
        resp = await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                               timeout=120_000)
        info(f"HTTP {resp.status if resp else '?'}; booting {args.wait:.0f}s")
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)
        st = await EitaaDriver(session).login_state()
        if not st.get("logged_in"):
            bad(f"not logged in ({st.get('auth_state')}) - log in first")
            _save()
            return 2
        ok(f"logged in: my_id={st.get('my_id')}")

        banner("STEP 2  -  snapshot the documents already here")
        first, err = await ev(page, LIST_DOCS_JS, int(args.per_chat), timeout=120)
        if err or not first or not first.get("ok"):
            bad(f"could not list documents: {(first or {}).get('code') or err}")
            _save()
            return 1
        seen = {(d["doc_id"]) for d in first.get("docs", [])}
        info(f"{len(seen)} document(s) already in the account")
        for d in first.get("docs", [])[:6]:
            info(f"  existing: {d.get('file_name')}  {d.get('mime')}  "
                 f"{d.get('size')}B  from={'me' if d.get('out') else 'other'}")

        new_docs = []
        if args.scan_only:
            banner("STEP 3  -  scan-only: dissecting the most recent documents")
            new_docs = first.get("docs", [])[:args.max_dissect]
        else:
            banner(f"STEP 3  -  WATCHING for {args.watch:.0f}s - trigger the other "
                   f"bot NOW")
            info("send the zip from the other bot to a contact or Saved Messages;")
            info("any NEW document that appears will be downloaded and dissected")
            deadline = time.time() + float(args.watch)
            while time.time() < deadline:
                await asyncio.sleep(float(args.poll))
                cur, e = await ev(page, LIST_DOCS_JS, int(args.per_chat), timeout=120)
                if e or not cur or not cur.get("ok"):
                    continue
                for d in cur.get("docs", []):
                    if d["doc_id"] not in seen:
                        seen.add(d["doc_id"])
                        new_docs.append(d)
                        hit(f"NEW document: {d.get('file_name')}  {d.get('mime')}  "
                            f"{d.get('size')}B  from={'me' if d.get('out') else 'other'}")
                left = int(deadline - time.time())
                if new_docs and args.stop_on_first:
                    break
                if left % 20 == 0 and left > 0:
                    info(f"  still watching... {left}s left, {len(new_docs)} new so far")
            if not new_docs:
                info("no new document appeared in the watch window")

        banner("STEP 4  -  download + dissect")
        if not new_docs:
            info("nothing to dissect. If the other bot already sent earlier, re-run "
                 "with --scan-only to pull apart the most recent files.")
        for d in new_docs[:args.max_dissect]:
            banner(f"DOC  {d.get('file_name')}  ({d.get('mime')}, {d.get('size')}B)")
            dump("metadata Eitaa stores:", d)
            anal = await download_and_dissect(page, d, int(args.cap_kb) * 1024)
            REPORT["documents"].append({"meta": d, "dissection": anal})
            dump("dissection:", anal)
            # headline findings
            if anal.get("looks_zip"):
                names = anal.get("entry_names")
                hit(f"ZIP inside. entry names: {names}")
                if anal.get("has_apk_named_entry"):
                    hit("  -> an entry IS named *.apk, yet it was delivered. That "
                        "means EITHER this file was received (not sent through the "
                        "filter) OR the filter is off for this path - check "
                        "from='me'/'other' in the metadata above.")
                else:
                    hit(f"  -> NO entry named *.apk - the apk is hidden under a "
                        f"different inner name. THAT is the trick: {names}")
                if anal.get("any_encrypted"):
                    hit("  -> entries are ENCRYPTED (password-protected zip)")
            elif anal.get("magic_guess"):
                hit(f"not a zip - looks like {anal.get('magic_guess')}")

        try:
            await page.screenshot(path=str(_OUTDIR / "page.png"))
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        import traceback
        REPORT["traceback"] = traceback.format_exc()
        print(REPORT["traceback"], flush=True)
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass

    _verdict()
    _save()
    return 0


def _verdict() -> None:
    banner("VERDICT")
    v = REPORT["verdict"]
    zips = [d for d in REPORT["documents"] if d["dissection"].get("looks_zip")]
    trick = [d for d in zips if not d["dissection"].get("has_apk_named_entry")]
    if trick:
        hit("FOUND THE OTHER BOT'S TRICK:")
        for d in trick:
            meta = d["meta"]
            an = d["dissection"]
            v.append(f"outer file: {meta.get('file_name')} ({meta.get('mime')})")
            v.append(f"  inner entry names: {an.get('entry_names')}")
            v.append(f"  encrypted: {an.get('any_encrypted')}")
            if an.get("nested"):
                v.append(f"  nested: {list(an['nested'].keys())}")
        v.append("Copy this EXACTLY into the bot: same outer name/extension, same "
                 "inner entry name(s), same encryption. That is the recipe that "
                 "gets an apk past Eitaa, proven by a working bot.")
    elif zips:
        v.append("The documents seen are zips but each has a *.apk entry inside. "
                 "If those were RECEIVED (from='other'), they came in without "
                 "passing the send filter, so they do not reveal the trick. Make "
                 "the other bot SEND one (from='me') and watch that.")
    else:
        v.append("No zip-carrying document was captured. Re-run with --watch while "
                 "the other bot sends, or --scan-only if it already sent, and make "
                 "sure the file it sends actually lands in a chat this account "
                 "can see.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)


def _save() -> None:
    try:
        (_OUTDIR / "report.json").write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"\n  full report + raw files: {_OUTDIR}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Spy on documents another bot sends on this Eitaa account, "
                    "and dissect each. Read-only, own account.")
    p.add_argument("--account", required=True)
    p.add_argument("--watch", type=float, default=180,
                   help="seconds to watch for new documents (default 180)")
    p.add_argument("--scan-only", action="store_true",
                   help="dissect the most recent existing documents, no waiting")
    p.add_argument("--poll", type=float, default=5, help="poll interval seconds")
    p.add_argument("--per-chat", type=int, default=8,
                   help="messages to scan per chat (default 8)")
    p.add_argument("--max-dissect", type=int, default=6,
                   help="how many documents to download+dissect (default 6)")
    p.add_argument("--cap-kb", type=float, default=1024,
                   help="max KB to download per file for structure (default 1024)")
    p.add_argument("--stop-on-first", action="store_true",
                   help="stop watching as soon as one new document appears")
    p.add_argument("--wait", type=float, default=20)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
