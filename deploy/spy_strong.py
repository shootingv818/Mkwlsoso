#!/usr/bin/env python3
"""Stronger spy + dissect: catch the other bot's file the instant it is sent,
with the freshest file_reference, then read its head+tail with a LADDER of
download strategies and dump RAW bytes - so even a deliberately-malformed
("broken") zip reveals its structure, and we learn exactly WHY it will not
download.

Why stronger
------------
- The already-received copy is "broken" / won't download. A freshly-sent copy
  has a brand-new file_reference and is the best shot at reading any bytes.
- We never pull the whole 10 MB (that trips RETRY_LIMIT on the multi-part
  path). We read only the FIRST and LAST ~512 KB as single-part upload.getFile
  calls, each tried SIX different ways (precise / cdn / dc-routing / smaller
  limits) until one returns bytes - and we record every error if none do.
- We hex-dump the raw head and tail. A normal zip shows PK\x03\x04 at offset 0
  and PK\x01\x02 + PK\x05\x06 at the end. A "broken"/evasion zip will look
  different (prepended junk, data-descriptors, a central directory whose offset
  lies - that malformation IS the recipe), and the hex shows it.

How to use
----------
Start THIS first, then log the other bot into the account and have it send the
file. We seed the "already seen" set at startup, so we react to the NEW send.

    cd ~/Mkwlsoso && .venv/bin/python deploy/spy_strong.py --account 989939094655 --watch 240

Flags:
    --watch N          seconds to wait for a new send (default 240)
    --name-like S      only match files whose name contains S
    --any              match ANY new document, not just zips
    --include-existing dissect a matching file already in history (no waiting)

Read-only, own account.
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
LIMIT = 524288  # 512KB: multiple of 4096 and a divisor of 1MB -> one getFile
#                 part that never crosses a 1MB boundary (no RETRY_LIMIT).


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


# ---- JS: scan self + recent dialogs for every document currently visible ----
SCAN_DOCS_JS = r"""
async (a) => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
  let peers=[{_:"inputPeerSelf", __d:"self"}];
  try{
    const d=await AM.invokeApi("messages.getDialogs",{offset_date:0,offset_id:0,
      offset_peer:{_:"inputPeerEmpty"},limit:20,hash:0,exclude_pinned:false,folder_id:0});
    const uById={}; for(const u of (d.users||[])) uById[u.id]=u;
    const cById={}; for(const c of (d.chats||[])) cById[c.id]=c;
    for(const dl of (d.dialogs||[])){ const p=dl.peer; try{
      if(p._==="peerUser"&&uById[p.user_id]){const u=uById[p.user_id];peers.push({_:"inputPeerUser",user_id:u.id,access_hash:u.access_hash,__d:("user:"+(u.username||u.id))});}
      else if(p._==="peerChannel"&&cById[p.channel_id]){const c=cById[p.channel_id];peers.push({_:"inputPeerChannel",channel_id:c.id,access_hash:c.access_hash,__d:("chan:"+(c.title||c.id))});}
      else if(p._==="peerChat"&&p.chat_id){peers.push({_:"inputPeerChat",chat_id:p.chat_id,__d:("chat:"+p.chat_id)});}
    }catch(e){} }
  }catch(e){}
  function frhex(fr){try{if(!fr)return null;const b=new Uint8Array(fr);let s="";for(let i=0;i<b.length;i++)s+=b[i].toString(16).padStart(2,"0");return s;}catch(e){return null;}}
  const out=[];
  for(const peer of peers.slice(0,20)){
    try{
      const pc=Object.assign({},peer); delete pc.__d;
      const h=await AM.invokeApi("messages.getHistory",{peer:pc,offset_id:0,offset_date:0,add_offset:0,limit:12,max_id:0,min_id:0,hash:0});
      for(const m of (h.messages||[])){
        const doc=m.media&&m.media.document; if(!doc) continue;
        let fn=null, attrs=[];
        for(const at of (doc.attributes||[])){ attrs.push(at._); if(at._==="documentAttributeFilename") fn=at.file_name; }
        const isOut = !!(m.pFlags && m.pFlags.out) || !!m.out;
        out.push({msg_id:m.id, peer:peer.__d, out:isOut, file_name:fn, mime:doc.mime_type,
          size:String(doc.size), dc_id:doc.dc_id, doc_id:String(doc.id),
          access_hash:String(doc.access_hash), file_reference:frhex(doc.file_reference),
          attributes:attrs, has_thumb:!!(doc.thumbs&&doc.thumbs.length)});
      }
    }catch(e){}
  }
  return {ok:true, docs:out};
}
"""

# ---- JS: read ONE byte-range, trying six strategies until bytes come back ----
RANGE_MULTI_JS = r"""
async (a) => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
  function hx2u(h){ if(!h) return new Uint8Array(0); const u=new Uint8Array(h.length/2); for(let i=0;i<u.length;i++) u[i]=parseInt(h.substr(i*2,2),16); return u; }
  function ab2b64(b){ let s=""; for(let i=0;i<b.length;i++) s+=String.fromCharCode(b[i]); return btoa(s); }
  const loc={_:"inputDocumentFileLocation", id:a.doc_id, access_hash:a.access_hash,
             file_reference:hx2u(a.file_reference), thumb_size:""};
  let off=a.offset|0; off=off-(off%4096);
  const lim=a.limit||524288;
  const tries=[
    {p:{location:loc,offset:off,limit:lim,precise:false,cdn_supported:false}, o:{dcId:a.dc_id}, t:"plain+dc"},
    {p:{location:loc,offset:off,limit:lim,precise:true,cdn_supported:false}, o:{dcId:a.dc_id}, t:"precise+dc"},
    {p:{location:loc,offset:off,limit:lim,precise:false,cdn_supported:true}, o:{dcId:a.dc_id}, t:"cdn+dc"},
    {p:{location:loc,offset:off,limit:lim,precise:false,cdn_supported:false}, o:null, t:"plain+route"},
    {p:{location:loc,offset:off,limit:65536,precise:false,cdn_supported:false}, o:{dcId:a.dc_id}, t:"64k+dc"},
    {p:{location:loc,offset:off,limit:4096,precise:true,cdn_supported:false}, o:{dcId:a.dc_id}, t:"4k+precise"}
  ];
  const errs=[];
  for(const tr of tries){
    try{
      const part = tr.o ? await AM.invokeApi("upload.getFile", tr.p, tr.o)
                        : await AM.invokeApi("upload.getFile", tr.p);
      const b = part && part.bytes ? new Uint8Array(part.bytes) : null;
      if(b && b.length) return {ok:true, b64:ab2b64(b), bytes:b.length, offset:off, strategy:tr.t, type:(part._||null)};
      errs.push(tr.t+":0bytes");
    }catch(e){
      let s="ERR"; try{ s=String((e&&(e.type||e.error_message||e.message||e.code))||e); }catch(x){}
      errs.push(tr.t+":"+s);
    }
  }
  return {ok:false, offset:off, errors:errs};
}
"""


# -------------------------- zip structure parsing ---------------------------
def parse_local_headers(data: bytes) -> list:
    names, i, sig = [], 0, b"PK\x03\x04"
    while True:
        j = data.find(sig, i)
        if j == -1:
            break
        try:
            if j + 30 <= len(data):
                flags = struct.unpack_from("<H", data, j + 6)[0]
                method = struct.unpack_from("<H", data, j + 8)[0]
                nlen = struct.unpack_from("<H", data, j + 26)[0]
                name = data[j + 30:j + 30 + nlen]
                if name and j + 30 + nlen <= len(data):
                    names.append({"name": name.decode("utf-8", "replace"),
                                  "encrypted": bool(flags & 0x1),
                                  "data_descriptor": bool(flags & 0x8),
                                  "method": method, "at": j})
        except Exception:  # noqa: BLE001
            pass
        i = j + 4
    return names


def parse_central_directory(data: bytes) -> list:
    names, i, sig = [], 0, b"PK\x01\x02"
    while True:
        j = data.find(sig, i)
        if j == -1:
            break
        try:
            if j + 46 <= len(data):
                flags = struct.unpack_from("<H", data, j + 8)[0]
                method = struct.unpack_from("<H", data, j + 10)[0]
                nlen = struct.unpack_from("<H", data, j + 28)[0]
                lho = struct.unpack_from("<I", data, j + 42)[0]
                name = data[j + 46:j + 46 + nlen]
                if name and j + 46 + nlen <= len(data):
                    names.append({"name": name.decode("utf-8", "replace"),
                                  "encrypted": bool(flags & 0x1),
                                  "method": method, "local_header_offset": lho,
                                  "at": j})
        except Exception:  # noqa: BLE001
            pass
        i = j + 4
    return names


def parse_eocd(data: bytes):
    j = data.rfind(b"PK\x05\x06")
    if j == -1 or j + 22 > len(data):
        return None
    try:
        (_disk, _cdd, _nd, n_total, cd_size, cd_off, clen) = struct.unpack_from(
            "<HHHHIIH", data, j + 4)
        return {"pos_in_slice": j, "entries_total": n_total, "cd_size": cd_size,
                "cd_offset": cd_off, "comment_len": clen}
    except Exception:  # noqa: BLE001
        return None


_SIGS = {b"PK\x03\x04": "local", b"PK\x01\x02": "central", b"PK\x05\x06": "eocd",
         b"PK\x07\x08": "datadesc", b"PK\x06\x06": "zip64eocd",
         b"PK\x06\x07": "zip64loc"}


def scan_all_sigs(data: bytes) -> list:
    found = []
    for sig, name in _SIGS.items():
        i = 0
        while True:
            j = data.find(sig, i)
            if j == -1:
                break
            found.append((j, name))
            i = j + 4
    found.sort()
    return found


def hexdump(data: bytes, n: int, tail: bool = False) -> str:
    chunk = data[-n:] if tail else data[:n]
    out = []
    base = (len(data) - len(chunk)) if tail else 0
    for off in range(0, len(chunk), 16):
        row = chunk[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in row)
        asci = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append(f"    {base + off:08x}  {hexs:<47}  {asci}")
    return "\n".join(out)


# ------------------------------ fetch helpers -------------------------------
async def fetch_range(page, doc, offset, limit, label):
    arg = {"doc_id": doc["doc_id"], "access_hash": doc["access_hash"],
           "file_reference": doc.get("file_reference"), "dc_id": doc.get("dc_id"),
           "offset": offset, "limit": limit}
    r, err = await ev(page, RANGE_MULTI_JS, arg, timeout=90)
    if err or not r:
        bad(f"{label}: {err or 'no result'}")
        return None, None
    if not r.get("ok"):
        bad(f"{label}: all strategies failed")
        for e in (r.get("errors") or []):
            info(f"    - {e}")
        return None, (r.get("errors") or [])
    ok(f"{label}: {r['bytes']} bytes via [{r['strategy']}] at offset {r['offset']}")
    return base64.b64decode(r["b64"]), r["strategy"]


async def fetch_tail(page, doc, size, want, label):
    if size <= 0:
        return None
    k_last = (size - 1) // LIMIT
    k_first = max(0, size - want) // LIMIT
    chunks = []
    for k in range(k_first, k_last + 1):
        off = k * LIMIT
        part, _strat = await fetch_range(page, doc, off, LIMIT, f"{label} win#{k} @{off}")
        if part is None:
            break
        chunks.append(part)
    return b"".join(chunks) if chunks else None


# --------------------------------- picking ----------------------------------
def _sz(d):
    try:
        return int(d.get("size") or 0)
    except Exception:  # noqa: BLE001
        return 0


def pick_target(docs, name_like, any_doc):
    if not docs:
        return None
    if name_like:
        m = [d for d in docs if d.get("file_name") and name_like in d["file_name"]]
        return max(m, key=_sz) if m else None
    if any_doc:
        return max(docs, key=_sz)
    zips = [d for d in docs if d.get("mime") == "application/zip"]
    if zips:
        return max(zips, key=_sz)
    return max(docs, key=_sz)  # fallback: biggest document of any kind


# --------------------------------- dissect ----------------------------------
async def dissect(page, doc, config):
    size = int(doc["size"])
    banner("DISSECT  -  read head + tail, dump raw bytes")
    ok(f"target: {doc.get('file_name')!r}  {size} B  {doc.get('mime')}  "
       f"dc={doc.get('dc_id')}  out={doc.get('out')}  peer={doc.get('peer')}")
    info(f"doc_id={doc.get('doc_id')}  access_hash={doc.get('access_hash')}")
    info(f"attributes={doc.get('attributes')}  has_thumb={doc.get('has_thumb')}")

    banner("HEAD (first 512 KB  -  local file headers)")
    head, _ = await fetch_range(page, doc, 0, LIMIT, "head")
    banner("TAIL (last 512 KB  -  central directory + EOCD, ends at EOF)")
    tail = await fetch_tail(page, doc, size, LIMIT, "tail")

    result = {"file_name": doc.get("file_name"), "size": size,
              "mime": doc.get("mime"), "attributes": doc.get("attributes"),
              "out": doc.get("out"), "peer": doc.get("peer")}
    anomalies = []

    if head:
        banner("HEAD raw (first 160 bytes)")
        print(hexdump(head, 160), flush=True)
        lh = parse_local_headers(head)
        sigs = scan_all_sigs(head)
        result["local_headers"] = lh
        result["head_sigs"] = [(p, n) for p, n in sigs][:20]
        if sigs and sigs[0][0] != 0:
            anomalies.append(f"first PK signature is at offset {sigs[0][0]}, not 0 "
                             f"- junk/stub prepended (self-extracting or evasion)")
        if not sigs:
            anomalies.append("NO PK signature in first 512KB - not a standard zip front")
        if lh:
            hit(f"LOCAL names: {[e['name'] for e in lh]}")
            for e in lh:
                info(f"    {e['name']!r}  enc={e['encrypted']}  "
                     f"method={e['method']}  data_descriptor={e['data_descriptor']}")
                if e["data_descriptor"]:
                    anomalies.append(f"'{e['name']}' uses a DATA DESCRIPTOR "
                                     f"(streamed zip; sizes after data)")

    if tail:
        banner("TAIL raw (last 320 bytes)")
        print(hexdump(tail, 320, tail=True), flush=True)
        cd = parse_central_directory(tail)
        eocd = parse_eocd(tail)
        sigs = scan_all_sigs(tail)
        result["central_directory"] = cd
        result["eocd"] = eocd
        result["tail_sigs"] = [(p, n) for p, n in sigs][-20:]
        if cd:
            hit(f"CENTRAL names (what Eitaa reads): {[e['name'] for e in cd]}")
            for e in cd:
                info(f"    {e['name']!r}  enc={e['encrypted']}  method={e['method']}")
        else:
            anomalies.append("NO central-directory record (PK0102) in the tail - "
                             "Eitaa cannot list entries here (likely the trick)")
        if eocd:
            info(f"EOCD: entries={eocd['entries_total']}  cd_size={eocd['cd_size']}  "
                 f"cd_offset={eocd['cd_offset']}  comment_len={eocd['comment_len']}")
            if eocd["cd_offset"] > size:
                anomalies.append(f"EOCD central-dir offset {eocd['cd_offset']} > file "
                                 f"size {size} - the offset LIES (classic evasion)")
            if cd and eocd["entries_total"] != len(cd):
                anomalies.append(f"EOCD claims {eocd['entries_total']} entries but "
                                 f"{len(cd)} central records found")
        else:
            anomalies.append("NO EOCD (PK0506) found - the end-of-zip marker is "
                             "missing/mangled (parsers that need it will choke)")

    # cross-check names
    lh_names = [e["name"] for e in result.get("local_headers", [])]
    cd_names = [e["name"] for e in result.get("central_directory", [])]
    if lh_names and cd_names and set(lh_names) != set(cd_names):
        anomalies.append(f"LOCAL names {lh_names} != CENTRAL names {cd_names} "
                         f"- names were tampered (Eitaa reads one, phone the other)")

    banner("VERDICT")
    names = cd_names or lh_names
    if names:
        enc = any(e["encrypted"] for e in
                  (result.get("central_directory") or result.get("local_headers") or []))
        apk_named = any(n.lower().endswith(".apk") for n in names)
        print(f"\n  outer file : {doc.get('file_name')}  ({doc.get('mime')}, {size} B)", flush=True)
        print(f"  inner names: {names}", flush=True)
        print(f"  encrypted  : {enc}", flush=True)
        print(f"  apk-named  : {apk_named}", flush=True)
        if lh_names and cd_names and set(lh_names) != set(cd_names):
            print("  >> local vs central names DIFFER - this is the evasion: Eitaa", flush=True)
            print("     reads the central name, the phone extracts by local name.", flush=True)
        elif not apk_named:
            print(f"  >> the apk rides under a non-apk inner name {names}; Eitaa sees", flush=True)
            print("     no .apk and delivers. (Recipient still needs a one-tap path.)", flush=True)
        else:
            print("  >> inner entry IS .apk - if this passed the filter, the WAY it", flush=True)
            print("     passed is in the anomalies below, not the name.", flush=True)
    else:
        print("\n  No entry names parsed from head or tail.", flush=True)

    if anomalies:
        print("\n  ANOMALIES (how this zip differs from a normal one = the recipe):", flush=True)
        for a in anomalies:
            print(f"    * {a}", flush=True)
        result["anomalies"] = anomalies
    else:
        print("\n  No structural anomalies: it parses as a plain, well-formed zip.", flush=True)

    try:
        outdir = config.ARTIFACTS_DIR / f"spystrong_{time.strftime('%Y%m%d-%H%M%S')}"
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        if head:
            (outdir / "head.bin").write_bytes(head)
        if tail:
            (outdir / "tail.bin").write_bytes(tail)
        print(f"\n  saved head/tail + result.json to {outdir}", flush=True)
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------- run ------------------------------------
async def run(args) -> int:
    from config import config
    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver

    banner("SPY STRONG  -  catch the fresh send, read head+tail, dump raw bytes")
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

        banner("INVENTORY  -  documents currently in history (self + 20 dialogs)")
        r, err = await ev(page, SCAN_DOCS_JS, {}, timeout=120)
        raw = (r or {}).get("docs") or []
        if err or not (r or {}).get("ok"):
            bad(f"could not list documents: {(r or {}).get('code') or err}")
            return 1
        uniq = {}  # dedupe by doc_id (same file forwarded shows up repeatedly)
        for d in raw:
            uniq.setdefault(d["doc_id"], d)
        docs = sorted(uniq.values(), key=_sz, reverse=True)
        if not docs:
            bad("no documents visible. Make sure the bot already SENT the file to a "
                "chat (or Saved Messages) this account can see, then re-run.")
            return 1
        for d in docs:
            info(f"{_sz(d):>10} B  {str(d.get('mime')):<24} out={d.get('out')}  "
                 f"peer={d.get('peer')}  name={d.get('file_name')!r}")

        target = None
        if args.watch and args.watch > 0:
            seen = {d["doc_id"] for d in docs}
            banner(f"WATCHING {args.watch:.0f}s for a NEW send - trigger the bot now")
            deadline = time.time() + float(args.watch)
            while time.time() < deadline:
                await asyncio.sleep(2.0)
                r2, _ = await ev(page, SCAN_DOCS_JS, {}, timeout=120)
                pool = [d for d in ((r2 or {}).get("docs") or [])
                        if d["doc_id"] not in seen]
                for d in pool:
                    seen.add(d["doc_id"])
                target = pick_target(pool, args.name_like, args.any)
                if target:
                    hit(f"caught NEW: {target.get('file_name')!r}  {_sz(target)}B")
                    break
            if not target:
                info("no new send appeared; dissecting the best EXISTING file instead")

        if not target:
            target = pick_target(docs, args.name_like, args.any)
        if not target:
            bad("no candidate matched. Pass --any (match any doc) or --name-like S.")
            return 1

        hit(f"dissecting: {target.get('file_name')!r}  {_sz(target)}B  "
            f"{target.get('mime')}")
        await dissect(page, target, config)
        return 0
    except Exception:  # noqa: BLE001
        import traceback
        banner("ERROR  -  scan crashed, full traceback below")
        print(traceback.format_exc(), flush=True)
        return 3
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    p = argparse.ArgumentParser(description="Catch a fresh send and dissect it hard.")
    p.add_argument("--account", required=True)
    p.add_argument("--watch", type=float, default=0,
                   help="0 (default) = dissect the file already in history now; "
                        ">0 = also wait that many seconds for a brand-new send")
    p.add_argument("--name-like", default=None, help="only match names containing this")
    p.add_argument("--any", action="store_true",
                   help="match ANY document, not just zips (pick the biggest)")
    p.add_argument("--wait", type=float, default=20, help="seconds to let the app settle")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
