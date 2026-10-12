#!/usr/bin/env python3
"""Read a big zip's INNER entry names from its head+tail only (no full download).

Why
---
The spy caught the other bot's working file: a 10 MB application/zip named
'عکـس_ها ی_یـاد گـار.zip', sent (from=me) so it passed Eitaa's filter. But
downloading it to inspect failed with RETRY_LIMIT - and we learned why: on this
build upload.getFile serves a SMALL (single-part) file instantly, but a
multi-part 10 MB file returns RETRY_LIMIT. It is SIZE, not the dc.

We do not need the payload. A zip stores its entry NAMES twice: in the local
file headers at the FRONT (PK\x03\x04) and in the central directory at the END
(PK\x01\x02). Eitaa's filter reads those names; so do we, by fetching only the
first and last ~512 KB - each a single-part upload.getFile that does NOT trip
RETRY_LIMIT - and parsing the names out. That reveals the other bot's exact
recipe: the inner entry name(s), whether they are encrypted, the compression
method - without pulling 10 MB.

Point it at the file by caption match (default: the Persian 'yadegar' name) or
an explicit --doc-id/--access-hash from the spy's metadata.

Read-only, own account.

    cd ~/Mkwlsoso && .venv/bin/python deploy/dissect_bigzip.py --account 989939094655
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import struct
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_T0 = time.time()


def banner(t): print("\n" + "=" * 76 + f"\n  {t}\n" + "=" * 76, flush=True)
def log(m=""): print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)
def ok(m): log(f"OK    {m}")
def bad(m): log(f"FAIL  {m}")
def info(m): log(f"      {m}")
def hit(m): log(f"####  {m}")


async def ev(page, js, arg=None, timeout=90):
    try:
        if arg is not None:
            return (await asyncio.wait_for(page.evaluate(js, arg), timeout=timeout), None)
        return (await asyncio.wait_for(page.evaluate(js), timeout=timeout), None)
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# Find the target document, then fetch a byte RANGE via upload.getFile at an
# explicit offset. A single 512KB part at any offset is one request -> no
# RETRY_LIMIT (which only hit the multi-part full download).
FIND_JS = r"""
async (a) => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
  // scan dialogs + self for the biggest matching zip
  let peers = [{_:"inputPeerSelf"}];
  try {
    const d = await AM.invokeApi("messages.getDialogs",{offset_date:0,offset_id:0,
      offset_peer:{_:"inputPeerEmpty"},limit:20,hash:0,exclude_pinned:false,folder_id:0});
    const uById={}; for(const u of (d.users||[])) uById[u.id]=u;
    const cById={}; for(const c of (d.chats||[])) cById[c.id]=c;
    for(const dl of (d.dialogs||[])){ const p=dl.peer; try{
      if(p._==="peerUser"&&uById[p.user_id]){const u=uById[p.user_id];peers.push({_:"inputPeerUser",user_id:u.id,access_hash:u.access_hash});}
      else if(p._==="peerChannel"&&cById[p.channel_id]){const c=cById[p.channel_id];peers.push({_:"inputPeerChannel",channel_id:c.id,access_hash:c.access_hash});}
    }catch(e){} }
  } catch(e){}
  let best=null;
  for(const peer of peers.slice(0,25)){
    try{
      const h=await AM.invokeApi("messages.getHistory",{peer,offset_id:0,offset_date:0,add_offset:0,limit:15,max_id:0,min_id:0,hash:0});
      for(const m of (h.messages||[])){
        const d=m.media&&m.media.document; if(!d) continue;
        let fn=null; for(const at of (d.attributes||[])) if(at._==="documentAttributeFilename") fn=at.file_name;
        const match = a.doc_id ? (String(d.id)===String(a.doc_id))
                               : (fn && a.caption_like && fn.indexOf(a.caption_like)>=0);
        const bigzip = (d.mime_type==="application/zip") && (+d.size > (a.min_size||1000000));
        if (match || (!a.doc_id && !a.caption_like && bigzip)) {
          const cand={file_name:fn, mime:d.mime_type, size:String(d.size),
            dc_id:d.dc_id, doc_id:String(d.id), access_hash:String(d.access_hash),
            file_reference:(function(fr){try{if(!fr)return null;const b=new Uint8Array(fr);let h="";for(let i=0;i<b.length;i++)h+=b[i].toString(16).padStart(2,"0");return h;}catch(e){return null;}})(d.file_reference)};
          if(!best || +cand.size>+best.size) best=cand;
        }
      }
    }catch(e){}
  }
  return {ok:!!best, doc:best};
}
"""

RANGE_JS = r"""
async (a) => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
  function hx2u(h){ if(!h) return new Uint8Array(0); const u=new Uint8Array(h.length/2); for(let i=0;i<u.length;i++) u[i]=parseInt(h.substr(i*2,2),16); return u; }
  function ab2b64(b){ let s=""; for(let i=0;i<b.length;i++) s+=String.fromCharCode(b[i]); return btoa(s); }
  const loc={_:"inputDocumentFileLocation", id:a.doc_id, access_hash:a.access_hash,
             file_reference:hx2u(a.file_reference), thumb_size:""};
  // offset must be a multiple of 4096 for upload.getFile
  let off = a.offset|0; off = off - (off % 4096);
  try{
    const part = await AM.invokeApi("upload.getFile",
      {location:loc, offset:off, limit:a.limit||524288, precise:false, cdn_supported:false},
      {dcId:a.dc_id});
    const b = part && part.bytes ? new Uint8Array(part.bytes) : null;
    if(!b || !b.length) return {ok:false, code:"0 bytes at offset "+off};
    return {ok:true, b64:ab2b64(b), bytes:b.length, offset:off};
  }catch(e){
    let s="ERR"; try{ s=String((e&&(e.type||e.error_message||e.message||e.code))||e);}catch(x){}
    return {ok:false, code:s, offset:off};
  }
}
"""


def parse_central_directory(data: bytes) -> list:
    """Pull entry names from central-directory records (PK\x01\x02) found
    anywhere in `data`. Works on a tail slice."""
    names = []
    i = 0
    sig = b"PK\x01\x02"
    while True:
        j = data.find(sig, i)
        if j == -1:
            break
        try:
            # central dir header: name len at +28 (2), extra +30, comment +32,
            # name starts at +46
            if j + 46 > len(data):
                break
            nlen = struct.unpack_from("<H", data, j + 28)[0]
            flags = struct.unpack_from("<H", data, j + 8)[0]
            method = struct.unpack_from("<H", data, j + 10)[0]
            name = data[j + 46:j + 46 + nlen]
            if name and j + 46 + nlen <= len(data):
                names.append({"name": name.decode("utf-8", "replace"),
                              "encrypted": bool(flags & 0x1), "method": method})
        except Exception:  # noqa: BLE001
            pass
        i = j + 4
    return names


def parse_local_headers(data: bytes) -> list:
    """Pull entry names from local file headers (PK\x03\x04) at the front."""
    names = []
    i = 0
    sig = b"PK\x03\x04"
    while True:
        j = data.find(sig, i)
        if j == -1:
            break
        try:
            if j + 30 > len(data):
                break
            flags = struct.unpack_from("<H", data, j + 6)[0]
            method = struct.unpack_from("<H", data, j + 8)[0]
            nlen = struct.unpack_from("<H", data, j + 26)[0]
            name = data[j + 30:j + 30 + nlen]
            if name and j + 30 + nlen <= len(data):
                names.append({"name": name.decode("utf-8", "replace"),
                              "encrypted": bool(flags & 0x1), "method": method})
        except Exception:  # noqa: BLE001
            pass
        i = j + 4
    return names


LIMIT = 524288  # 512KB: multiple of 4096 AND a divisor of 1MB -> a single
#                 getFile part that never crosses a 1MB boundary (so it is one
#                 request -> no RETRY_LIMIT, which only hit the multi-part full
#                 download).


async def fetch_range(page, doc, offset, limit, label):
    arg = {"doc_id": doc["doc_id"], "access_hash": doc["access_hash"],
           "file_reference": doc.get("file_reference"), "dc_id": doc.get("dc_id"),
           "offset": offset, "limit": limit}
    r, err = await ev(page, RANGE_JS, arg, timeout=60)
    if err or not r or not r.get("ok"):
        bad(f"{label}: {(r or {}).get('code') or err}")
        return None
    ok(f"{label}: got {r['bytes']} bytes at offset {r['offset']}")
    return base64.b64decode(r["b64"])


async def fetch_tail(page, doc, size, want, label):
    """Fetch at least `want` bytes ending exactly at EOF, as LIMIT-aligned
    windows concatenated together.

    Aligning every window offset to a multiple of LIMIT keeps each request
    valid (multiple of 4096, never crossing a 1MB boundary) AND lets the final
    window run to EOF - a plain `size - LIMIT` offset, rounded down to 4096,
    stops short of EOF and misses the central directory + EOCD that live in the
    last handful of bytes.
    """
    if size <= 0:
        return None
    k_last = (size - 1) // LIMIT
    k_first = max(0, size - want) // LIMIT
    chunks = []
    for k in range(k_first, k_last + 1):
        off = k * LIMIT
        part = await fetch_range(page, doc, off, LIMIT, f"{label} win#{k} @{off}")
        if part is None:
            break  # a hole breaks contiguity; keep the contiguous prefix we have
        chunks.append(part)
    return b"".join(chunks) if chunks else None


async def run(args) -> int:
    from config import config
    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver

    banner("DISSECT BIG ZIP  -  read entry names from head+tail, no full download")
    session = await BrowserSession(args.account, headed=False, light_assets=None).start()
    page = session.page
    try:
        await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)
        st = await EitaaDriver(session).login_state()
        if not st.get("logged_in"):
            bad(f"not logged in ({st.get('auth_state')})")
            return 2
        ok(f"logged in: my_id={st.get('my_id')}")

        banner("STEP 1  -  find the target file")
        find_arg = {"doc_id": args.doc_id, "caption_like": args.name_like,
                    "min_size": 1000000}
        r, err = await ev(page, FIND_JS, find_arg, timeout=90)
        if err or not r or not r.get("ok"):
            bad(f"target not found: {(r or {}).get('code') or err}")
            info("pass --name-like <substring> or --doc-id to target it exactly")
            return 1
        doc = r["doc"]
        size = int(doc["size"])
        ok(f"target: {doc['file_name']}  {size} bytes  dc={doc['dc_id']}  "
           f"doc_id={doc['doc_id']}")

        banner("STEP 2  -  fetch the HEAD (local file headers)")
        head = await fetch_range(page, doc, 0, LIMIT, "head 0..512KB")

        banner("STEP 3  -  fetch the TAIL (central directory, ends at EOF)")
        tail = await fetch_tail(page, doc, size, LIMIT, "tail")

        banner("STEP 4  -  parse the entry names")
        result = {"file_name": doc["file_name"], "size": size}
        if head:
            lh = parse_local_headers(head)
            result["local_headers"] = lh
            if lh:
                hit(f"LOCAL headers (front of file): {[e['name'] for e in lh]}")
                for e in lh:
                    info(f"    name={e['name']!r}  encrypted={e['encrypted']}  "
                         f"method={e['method']}")
        if tail:
            cd = parse_central_directory(tail)
            result["central_directory"] = cd
            if cd:
                hit(f"CENTRAL directory (end of file, what Eitaa reads): "
                    f"{[e['name'] for e in cd]}")
                for e in cd:
                    info(f"    name={e['name']!r}  encrypted={e['encrypted']}  "
                         f"method={e['method']}")

        banner("VERDICT  -  the other bot's exact recipe")
        allnames = []
        for e in (result.get("central_directory") or result.get("local_headers") or []):
            allnames.append(e)
        if allnames:
            names = [e["name"] for e in allnames]
            enc = any(e["encrypted"] for e in allnames)
            apk_named = any(n.lower().endswith(".apk") for n in names)
            print(f"\n  outer file : {doc['file_name']}  (application/zip, {size} B)", flush=True)
            print(f"  inner names: {names}", flush=True)
            print(f"  encrypted  : {enc}", flush=True)
            print(f"  method     : {[e['method'] for e in allnames]} "
                  f"(0=store, 8=deflate, 99=AES)", flush=True)
            print("", flush=True)
            if not apk_named:
                print("  SO THE TRICK IS: the apk rides inside a normal .zip under a", flush=True)
                print(f"  non-apk inner name {names} - Eitaa reads that name, sees no", flush=True)
                print("  .apk, and delivers. The recipient opens the zip (every phone", flush=True)
                print("  does) and the file inside is the app.", flush=True)
                if enc:
                    print("  It is also ENCRYPTED - so a password is set; that both hides", flush=True)
                    print("  the content AND is why Eitaa cannot peek further.", flush=True)
                # the decisive detail for OUR recipients
                inner = names[0] if names else "?"
                print(f"\n  KEY QUESTION for your elderly recipients: the inner file is", flush=True)
                print(f"  named '{inner}'. If it ends in a non-apk extension, they must", flush=True)
                print(f"  rename or tap-install it. Check how the other bot's recipients", flush=True)
                print(f"  actually install it - that is the last piece. If '{inner}'", flush=True)
                print(f"  installs on tap, we copy this recipe exactly.", flush=True)
            else:
                print("  The inner entry IS named .apk - which means this file was", flush=True)
                print("  RECEIVED, not sent through the filter, OR Eitaa's filter is", flush=True)
                print("  off on this path. Verify it was from=me.", flush=True)
        else:
            print("  Could not parse entry names from head or tail. If both fetches", flush=True)
            print("  failed with RETRY_LIMIT even at single-part, the file's dc is", flush=True)
            print("  not serving us; try --name-like to pick a different copy, or", flush=True)
            print("  the file may need the appDownloadManager path.", flush=True)

        try:
            outdir = config.ARTIFACTS_DIR / f"bigzip_{time.strftime('%Y%m%d-%H%M%S')}"
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            if head:
                (outdir / "head.bin").write_bytes(head)
            if tail:
                (outdir / "tail.bin").write_bytes(tail)
            print(f"\n  saved head/tail + result to {outdir}", flush=True)
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Read a big zip's inner entry names from head+tail only.")
    p.add_argument("--account", required=True)
    p.add_argument("--name-like", default="یـاد",
                   help="substring of the target file name (default: the yadegar file)")
    p.add_argument("--doc-id", default=None, help="target this exact document id")
    p.add_argument("--wait", type=float, default=20)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
