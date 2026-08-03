#!/usr/bin/env python3
"""
FINAL PROBE -- close every remaining gap before the engine is written.

What earlier rounds already settled:
  dialog paging (stop on an EMPTY page: 608 dialogs, 601 PVs, page sizes
  [25,100,...]), folder_id is ignored by Eitaa, contacts.getContacts agrees with
  the contact/non-contact split (449/152), messages.search with a photo filter
  works, returned==0 reliably means "no photos here" (199 of 200 chats),
  pFlags.out separates sent from received, upload.getFile returns real bytes and
  is faster with no dc options, download concurrency 16 and search concurrency 8
  both ran without a single FLOOD_WAIT, there are no inline bytes so downloading
  is mandatory, searchGlobal is not in this build, and the DOM scroll is useless
  for counting.

What was still UNPROVEN, and is tested here:

  A  full photo census over ALL PVs      -- earlier runs capped at 200 of 601
  B  photo paging inside one chat        -- chats_needing_paging was 0 every
                                           time, so the offset_id paging path
                                           has never actually executed. Forced
                                           here with a small limit.
  C  file_reference stability            -- the project already fights expiry on
                                           the upload side (bridge_file_send.js
                                           refreshDoc). A long export could hit
                                           it on the download side.
  D  chunked download                    -- only a 12KB size was ever fetched,
                                           in one call
  E  sustained concurrency               -- 16 was measured once, not held
  F  base64 batch transfer               -- never measured; the earlier attempt
                                           was inside the probe that was killed
  G  PDF generation                      -- completely untested, and it is the
                                           actual deliverable. page.pdf() is
                                           Chromium-headless-only while jobs run
                                           headed, so a separate browser is used.
  H  delivering the PDF to Telegram      -- the bot has only ever sent text.
                                           OPT-IN via --send
  I  photos sent as uncompressed files   -- those are messageMediaDocument and
                                           the photo filter would miss them

Read-only against Eitaa (getDialogs, search, getFile). G writes a PDF under
ARTIFACTS_DIR. H is the only step that transmits anything, only to the owner's
own Telegram chat, and only when --send is passed.

Usage:
    cd ~/Mkwlsoso
    .venv/bin/python probe_final.py 989124089268
    .venv/bin/python probe_final.py 989124089268 --send     # also test delivery
    .venv/bin/python probe_final.py 989124089268 --quick    # skip the full census

Ctrl+C is safe: partial results are printed and saved.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONUNBUFFERED", "1")

from config import config  # noqa: E402
from capture.pool import pool as session_pool  # noqa: E402
from eitaa.driver import EitaaDriver  # noqa: E402

config.ensure_dirs()

OUT = Path("/tmp/photo_probe_final.json")
RESULT: dict = {"steps": {}}

SCAN_CONC = 8
DL_CONC = 16
SUSTAINED_N = 48          # how many downloads to hold at DL_CONC
STEP_TIMEOUT = 300


def save() -> None:
    try:
        OUT.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
    except OSError:
        pass


def show(name: str, data) -> None:
    RESULT["steps"][name] = data
    save()
    print(f"  [{name}]")
    if isinstance(data, dict):
        for k, v in data.items():
            if k.startswith("_b64"):
                continue
            if isinstance(v, list):
                print(f"      {k}: ({len(v)} items)")
                for item in v[:10]:
                    print(f"        {json.dumps(item, ensure_ascii=False, default=str)[:170]}")
                if len(v) > 10:
                    print(f"        ... +{len(v) - 10} more")
            else:
                print(f"      {k}: {json.dumps(v, ensure_ascii=False, default=str)[:170]}")
    print()


# ---------------------------------------------------------------------------
JS_SETUP = r"""
async (maxPages) => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, error: 'no apiManager' };
  const S = v => String(v);
  const users = new Map();
  const pv = [];
  let offset_date = 0, offset_id = 0, offset_peer = { _: 'inputPeerEmpty' };
  const sizes = [];
  try {
    for (let i = 0; i < maxPages; i++) {
      const d = await AM.invokeApi('messages.getDialogs', {
        folder_id: 0, offset_date, offset_id, offset_peer, limit: 100, hash: 0 });
      const dl = (d && d.dialogs) || [], us = (d && d.users) || [],
            ch = (d && d.chats) || [], ms = (d && d.messages) || [];
      sizes.push(dl.length);
      for (const u of us) if (u && u.id != null) users.set(S(u.id), u);
      for (const dlg of dl) {
        const p = dlg.peer || {};
        if (p._ !== 'peerUser') continue;
        const u = users.get(S(p.user_id));
        if (!u || u.access_hash == null) continue;
        const f = u.pFlags || {};
        if (f.self || f.bot || f.deleted) continue;
        pv.push({ id: S(u.id), access_hash: S(u.access_hash),
                  name: ((u.first_name||'')+' '+(u.last_name||'')).trim()
                        || (u.phone ? S(u.phone) : S(u.id)),
                  top_message: dlg.top_message || 0 });
      }
      if (dl.length === 0) break;
      const last = dl[dl.length - 1];
      const topId = last.top_message || 0;
      const lm = ms.find(m => m.id === topId);
      const pid = offset_id;
      offset_id = topId; offset_date = lm ? lm.date : offset_date;
      const lp = last.peer || {};
      if (lp._ === 'peerUser') {
        const u = users.get(S(lp.user_id));
        offset_peer = u && u.access_hash != null
          ? { _:'inputPeerUser', user_id:+u.id, access_hash:u.access_hash }
          : { _:'inputPeerEmpty' };
      } else if (lp._ === 'peerChannel') {
        const c = ch.find(x => S(x.id) === S(lp.channel_id));
        offset_peer = c && c.access_hash != null
          ? { _:'inputPeerChannel', channel_id:+c.id, access_hash:c.access_hash }
          : { _:'inputPeerEmpty' };
      } else if (lp._ === 'peerChat') {
        offset_peer = { _:'inputPeerChat', chat_id: lp.chat_id };
      } else break;
      if (offset_peer._ === 'inputPeerEmpty' || offset_id === pid) break;
    }
  } catch (e) { return { ok:false, error:String(e && (e.type||e.message)||e) }; }
  window.__MKWL_pv = pv;
  window.__MKWL_users = users;
  return { ok: true, pv_count: pv.length, page_sizes: sizes };
}
"""

JS_CENSUS = r"""
async (args) => {
  const AM = window.apiManager;
  const pv = window.__MKWL_pv || [];
  const list = args.cap ? pv.slice(0, args.cap) : pv;
  const isFlood = s => /FLOOD|TOO_MANY|LIMIT/i.test(String(s||''));
  const rows = []; let floods = 0;
  const t0 = performance.now();
  for (let i = 0; i < list.length; i += args.conc) {
    const batch = list.slice(i, i + args.conc);
    const rs = await Promise.all(batch.map(async p => {
      try {
        const r = await AM.invokeApi('messages.search', {
          peer: { _:'inputPeerUser', user_id:+p.id, access_hash:p.access_hash },
          q:'', filter:{_:'inputMessagesFilterPhotos'}, min_date:0, max_date:0,
          offset_id:0, add_offset:0, limit:100, max_id:0, min_id:0, hash:0 });
        const msgs = (r && r.messages) || [];
        const ph = msgs.filter(m => m.media && m.media.photo);
        return { name: p.name, id: p.id, reply: r && r._,
                 count: (r && r.count != null) ? r.count : null,
                 returned: msgs.length, photos: ph.length,
                 sent: ph.filter(m => m.pFlags && m.pFlags.out).length,
                 slice: !!(r && r._ === 'messages.messagesSlice') };
      } catch (e) {
        const s = String(e && (e.type||e.message)||e).slice(0,40);
        if (isFlood(s)) floods++;
        return { name: p.name, id: p.id, error: s };
      }
    }));
    rows.push(...rs);
    if (floods) break;
  }
  const ms = Math.round(performance.now() - t0);
  const withP = rows.filter(r => (r.photos||0) > 0);
  withP.sort((a,b) => (b.count||b.photos||0) - (a.count||a.photos||0));
  const declared = rows.reduce((a,r) =>
    a + (r.count != null ? r.count : (r.photos||0)), 0);
  window.__MKWL_biggest = withP.length ? withP[0] : null;
  return { ok: true, examined: rows.length, ms,
           ms_per_chat: rows.length ? Math.round(ms/rows.length) : null,
           chats_with_photos: withP.length,
           chats_zero: rows.filter(r => !r.error && (r.photos||0) === 0).length,
           photos_total_declared: declared,
           photos_first_page: rows.reduce((a,r)=>a+(r.photos||0),0),
           sent_by_me: rows.reduce((a,r)=>a+(r.sent||0),0),
           slices: rows.filter(r => r.slice).length,
           floods, errors: rows.filter(r => r.error).length,
           top: withP.slice(0,8).map(r => ({ name:(r.name||'').slice(0,22),
             photos:r.photos, declared:r.count, sent:r.sent, slice:r.slice })) };
}
"""

# B: force the paging path with a deliberately small limit
JS_PAGING = r"""
async (args) => {
  const AM = window.apiManager;
  const big = window.__MKWL_biggest;
  const users = window.__MKWL_users;
  if (!big) return { ok:false, error:'no chat with photos' };
  const u = users.get(big.id);
  const peer = { _:'inputPeerUser', user_id:+u.id, access_hash:u.access_hash };
  const seen = new Set(); const pages = [];
  let offset_id = 0; let firstReply = null;
  const t0 = performance.now();
  try {
    for (let pg = 0; pg < args.maxPages; pg++) {
      const r = await AM.invokeApi('messages.search', {
        peer, q:'', filter:{_:'inputMessagesFilterPhotos'}, min_date:0, max_date:0,
        offset_id, add_offset:0, limit:args.limit, max_id:0, min_id:0, hash:0 });
      if (!firstReply) firstReply = { type: r && r._, count: r && r.count };
      const msgs = (r && r.messages) || [];
      const ph = msgs.filter(m => m.media && m.media.photo);
      const before = seen.size;
      for (const m of ph) seen.add(m.id);
      pages.push({ page: pg+1, returned: msgs.length, photos: ph.length,
                   new_unique: seen.size - before, offset_used: offset_id });
      if (msgs.length === 0) break;
      const nextOffset = msgs[msgs.length - 1].id;
      if (nextOffset === offset_id) {
        pages.push({ note: 'offset did not advance -- would loop forever' });
        break;
      }
      offset_id = nextOffset;
      if (seen.size >= (firstReply.count || 0) && firstReply.count) break;
    }
  } catch (e) { return { ok:false, error:String(e && (e.type||e.message)||e), pages }; }
  return { ok: true, chat: (big.name||'').slice(0,22),
           declared_count: firstReply && firstReply.count,
           reply_type: firstReply && firstReply.type,
           limit_used: args.limit, pages_walked: pages.length,
           unique_photos_collected: seen.size, pages,
           ms: Math.round(performance.now() - t0),
           paging_works: seen.size > args.limit };
}
"""

# C + D + E: reference stability, chunking, sustained concurrency
JS_DOWNLOAD = r"""
async (args) => {
  const AM = window.apiManager;
  const big = window.__MKWL_biggest;
  const users = window.__MKWL_users;
  if (!big) return { ok:false, error:'no chat with photos' };
  const u = users.get(big.id);
  const peer = { _:'inputPeerUser', user_id:+u.id, access_hash:u.access_hash };
  const isFlood = s => /FLOOD|TOO_MANY|LIMIT/i.test(String(s||''));
  const out = {};

  // collect photos
  const r = await AM.invokeApi('messages.search', {
    peer, q:'', filter:{_:'inputMessagesFilterPhotos'}, min_date:0, max_date:0,
    offset_id:0, add_offset:0, limit:100, max_id:0, min_id:0, hash:0 });
  const photos = ((r && r.messages) || [])
    .filter(m => m.media && m.media.photo).map(m => m.media.photo);
  if (!photos.length) return { ok:false, error:'no photos' };
  window.__MKWL_photos = photos;

  const refOf = p => {
    const f = p.file_reference;
    if (!f) return null;
    const a = Array.from(f.length != null ? f : []);
    return a.slice(0, 12).join(',');
  };
  const pickSize = (p, target) => {
    const s = (p.sizes||[]).filter(x => x.type && x.w)
      .sort((a,b) => Math.abs(a.w-target) - Math.abs(b.w-target));
    return s.length ? s[0] : null;
  };
  async function grab(p, type, offset, limit) {
    const res = await AM.invokeApi('upload.getFile', {
      location: { _:'inputPhotoFileLocation', id:p.id, access_hash:p.access_hash,
                  file_reference:p.file_reference, thumb_size:type },
      offset: offset||0, limit: limit||1048576 });
    const b = res && res.bytes;
    return b ? (b.length || b.byteLength || 0) : 0;
  }

  // ---- C: is file_reference stable across two searches? ----
  try {
    const first = refOf(photos[0]);
    const r2 = await AM.invokeApi('messages.search', {
      peer, q:'', filter:{_:'inputMessagesFilterPhotos'}, min_date:0, max_date:0,
      offset_id:0, add_offset:0, limit:100, max_id:0, min_id:0, hash:0 });
    const p2 = ((r2 && r2.messages)||[]).filter(m => m.media && m.media.photo)
      .map(m => m.media.photo);
    const second = p2.length ? refOf(p2[0]) : null;
    const szc = pickSize(photos[0], 320);
    let refetch_ok = false, refetch_err = null;
    try { refetch_ok = (await grab(photos[0], szc.type)) > 100; }
    catch (e) { refetch_err = String(e && (e.type||e.message)||e).slice(0,50); }
    out.C_reference = { ok: true, first_ref_head: first, second_ref_head: second,
      reference_is_stable: first === second,
      redownload_with_original_ref_ok: refetch_ok, error: refetch_err,
      note: 'if the reference rotates, the engine must download soon after the '
            + 'search or re-search to refresh it' };
  } catch (e) {
    out.C_reference = { ok:false, error:String(e && (e.type||e.message)||e) };
  }

  // ---- D: chunked download of the LARGEST size ----
  try {
    const p = photos[0];
    const bigSize = (p.sizes||[]).filter(s => s.type && s.w)
      .sort((a,b) => (b.w||0)-(a.w||0))[0];
    const declared = bigSize.size || 0;
    const CH = 16384;
    let total = 0, chunks = 0;
    const t = performance.now();
    for (let off = 0; off < Math.max(declared, CH); off += CH) {
      const n = await grab(p, bigSize.type, off, CH);
      chunks++; total += n;
      if (n < CH) break;
      if (chunks > 40) break;
    }
    out.D_chunked = { ok: declared ? total >= declared : total > 0,
      size_type: bigSize.type, width: bigSize.w, declared_bytes: declared,
      downloaded_bytes: total, chunks, chunk_size: CH,
      matches_declared: total === declared,
      ms: Math.round(performance.now() - t) };
  } catch (e) {
    out.D_chunked = { ok:false, error:String(e && (e.type||e.message)||e).slice(0,60) };
  }

  // ---- E: sustained concurrency ----
  try {
    const szc = pickSize(photos[0], 320);
    const target = Math.min(args.sustained, photos.length);
    const rounds = [];
    let floods = 0, okAll = 0, bytesAll = 0;
    const t0 = performance.now();
    for (let i = 0; i < target; i += args.conc) {
      const batch = [];
      for (let k = 0; k < args.conc; k++)
        batch.push(photos[(i + k) % photos.length]);
      const t = performance.now();
      const rs = await Promise.all(batch.map(p =>
        grab(p, szc.type).catch(e =>
          'ERR:' + String(e && (e.type||e.message)||e).slice(0,30))));
      const nums = rs.filter(x => typeof x === 'number');
      const f = rs.filter(x => typeof x === 'string' && isFlood(x)).length;
      floods += f; okAll += nums.filter(b => b > 100).length;
      bytesAll += nums.reduce((a,b)=>a+b,0);
      rounds.push({ round: rounds.length+1, ms: Math.round(performance.now()-t),
                    ok: nums.filter(b=>b>100).length, floods: f,
                    err: rs.find(x => typeof x === 'string') || null });
      if (floods) break;
    }
    const ms = Math.round(performance.now() - t0);
    out.E_sustained = { ok: floods === 0 && okAll > 0, requested: target,
      downloads_ok: okAll, floods, total_ms: ms,
      ms_per_photo: okAll ? Math.round(ms/okAll) : null,
      throughput_kb_s: ms ? Math.round(bytesAll/1024/(ms/1000)) : 0,
      size_used: szc.type, concurrency: args.conc, rounds };
  } catch (e) {
    out.E_sustained = { ok:false, error:String(e && (e.type||e.message)||e).slice(0,60) };
  }

  // ---- I: photos sent as uncompressed documents ----
  try {
    const rd = await AM.invokeApi('messages.search', {
      peer, q:'', filter:{_:'inputMessagesFilterDocument'}, min_date:0, max_date:0,
      offset_id:0, add_offset:0, limit:100, max_id:0, min_id:0, hash:0 });
    const docs = ((rd && rd.messages)||[]).filter(m => m.media && m.media.document);
    const imgs = docs.filter(m => /^image\//i.test(
      (m.media.document.mime_type || '')));
    out.I_image_documents = { ok: true, documents_found: docs.length,
      image_mime_documents: imgs.length,
      samples: imgs.slice(0,5).map(m => ({ mime: m.media.document.mime_type,
        size: m.media.document.size, out: !!(m.pFlags && m.pFlags.out) })),
      note: 'these are photos sent as files; the photo filter does NOT see them' };
  } catch (e) {
    out.I_image_documents = { ok:false, error:String(e && (e.type||e.message)||e).slice(0,60) };
  }

  return { ok: true, probes: out };
}
"""

# F: base64 batch transfer
JS_B64 = r"""
async (args) => {
  const AM = window.apiManager;
  const photos = window.__MKWL_photos || [];
  if (!photos.length) return { ok:false, error:'no photos' };
  const pick = p => {
    const s = (p.sizes||[]).filter(x => x.type && x.w)
      .sort((a,b) => Math.abs(a.w-320) - Math.abs(b.w-320));
    return s.length ? s[0].type : 'm';
  };
  const t0 = performance.now();
  const list = [];
  for (let i = 0; i < args.n; i++) list.push(photos[i % photos.length]);
  const res = await Promise.all(list.map(async p => {
    try {
      const r = await AM.invokeApi('upload.getFile', {
        location: { _:'inputPhotoFileLocation', id:p.id, access_hash:p.access_hash,
                    file_reference:p.file_reference, thumb_size:pick(p) },
        offset:0, limit:1048576 });
      const u8 = r && r.bytes ? new Uint8Array(r.bytes) : new Uint8Array(0);
      let s = ''; const CH = 8192;
      for (let i = 0; i < u8.length; i += CH)
        s += String.fromCharCode.apply(null, u8.subarray(i, i+CH));
      return btoa(s);
    } catch (e) { return ''; }
  }));
  const fetch_ms = Math.round(performance.now() - t0);
  return { ok: true, n: res.length, fetch_ms,
           total_b64_chars: res.reduce((a,b)=>a+b.length,0),
           b64: args.keep ? res : [] };
}
"""


async def step(driver, label, js, arg=None, timeout=STEP_TIMEOUT):
    print(f"  ... {label}")
    t = time.time()
    try:
        coro = driver.page.evaluate(js, arg) if arg is not None \
            else driver.page.evaluate(js)
        res = await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        res = {"ok": False, "error": f"timed out after {timeout}s"}
    except Exception as exc:
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if isinstance(res, dict):
        res["_wall_s"] = round(time.time() - t, 1)
    show(label, res)
    return res


async def make_pdf(b64_list: list[str]) -> dict:
    """G: render the images to a PDF with a SEPARATE headless Chromium."""
    from playwright.async_api import async_playwright
    imgs = [b for b in b64_list if b]
    if not imgs:
        return {"ok": False, "error": "no images to render"}
    cells = "".join(
        f'<div class="c"><img src="data:image/jpeg;base64,{b}"></div>' for b in imgs)
    html = (
        "<html><head><meta charset='utf-8'><style>"
        "@page{size:A4;margin:8mm}"
        "body{margin:0;font:11px sans-serif}"
        ".g{display:grid;grid-template-columns:repeat(3,1fr);gap:4mm}"
        ".c{break-inside:avoid;text-align:center}"
        ".c img{width:100%;height:auto;border:1px solid #ccc}"
        "</style></head><body>"
        f"<h3>photo export test -- {len(imgs)} images</h3>"
        f"<div class='g'>{cells}</div></body></html>"
    )
    dest = Path(config.ARTIFACTS_DIR) / "probe_photo_export.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html, wait_until="load")
                await page.pdf(path=str(dest), format="A4",
                               print_background=True)
            finally:
                await browser.close()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "hint": "page.pdf() needs headless Chromium; a separate browser "
                        "is launched for exactly this reason"}
    size = dest.stat().st_size if dest.is_file() else 0
    return {"ok": size > 1000, "images": len(imgs), "pdf_path": str(dest),
            "pdf_bytes": size, "pdf_kb": round(size / 1024),
            "render_ms": round((time.time() - t0) * 1000),
            "ms_per_image": round((time.time() - t0) * 1000 / max(1, len(imgs)))}


async def send_pdf(path: str) -> dict:
    """H: deliver the PDF to the owner's own Telegram chat. Opt-in."""
    try:
        from telethon import TelegramClient
    except Exception as exc:
        return {"ok": False, "error": f"telethon missing: {exc}"}
    if not (config.API_ID and config.API_HASH and config.BOT_TOKEN):
        return {"ok": False, "error": "API_ID/API_HASH/BOT_TOKEN not configured"}
    sess = str(Path(config.DATA_DIR) / "probe_bot_session")
    t0 = time.time()
    try:
        client = TelegramClient(sess, int(config.API_ID), config.API_HASH)
        await client.start(bot_token=config.BOT_TOKEN)
        try:
            msg = await client.send_file(
                config.report_to(), path,
                caption="probe: photo export PDF (test)",
                force_document=True)
            return {"ok": True, "message_id": getattr(msg, "id", None),
                    "ms": round((time.time() - t0) * 1000)}
        finally:
            await client.disconnect()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def main(account: str, quick: bool, do_send: bool) -> int:
    print("=" * 70)
    print(f"  FINAL PROBE -- {account}")
    print("=" * 70)
    print()

    b64_keep: list[str] = []
    t0 = time.time()
    async with session_pool.lease(account, headed=config.HEADED_JOBS) as session:
        driver = EitaaDriver(session)
        await driver.open()
        print(f"  session ready in {time.time() - t0:.1f}s")
        if not await driver.is_logged_in():
            print("  FATAL: not logged in")
            return 1
        print()

        s = await step(driver, "0_setup", JS_SETUP, 40)
        if not s.get("ok"):
            return 1

        await step(driver, "A_census", JS_CENSUS,
                   {"conc": SCAN_CONC, "cap": 200 if quick else 0})
        await step(driver, "B_photo_paging", JS_PAGING,
                   {"limit": 5, "maxPages": 20})
        d = await step(driver, "CDE_I_download", JS_DOWNLOAD,
                       {"conc": DL_CONC, "sustained": SUSTAINED_N})
        if isinstance(d.get("probes"), dict):
            for k, v in d["probes"].items():
                show(k, v)

        print("  ... F_b64_transfer")
        rows = []
        for n in (4, 16, 32):
            try:
                t = time.time()
                r = await driver.page.evaluate(
                    JS_B64, {"n": n, "keep": n == 16})
                wall = round((time.time() - t) * 1000)
                if not r or not r.get("ok"):
                    print(f"        n={n}: {r}")
                    break
                if n == 16 and r.get("b64"):
                    b64_keep = [b for b in r["b64"] if b]
                row = {"n": r.get("n"), "in_page_ms": r.get("fetch_ms"),
                       "wall_ms": wall,
                       "bridge_overhead_ms": wall - (r.get("fetch_ms") or 0),
                       "b64_kb": round((r.get("total_b64_chars") or 0) / 1024),
                       "per_photo_ms": round(wall / max(1, r.get("n") or 1))}
                rows.append(row)
                print(f"        {json.dumps(row)}")
            except Exception as exc:
                print(f"        n={n} failed: {type(exc).__name__}: {exc}")
                break
        show("F_b64_transfer", {"ok": bool(rows), "rows": rows})

    print("  ... G_pdf_render (separate headless Chromium)")
    pdf = await make_pdf(b64_keep)
    show("G_pdf_render", pdf)

    if do_send and pdf.get("ok"):
        print("  ... H_telegram_delivery")
        print("      WARNING: this opens a SECOND connection with the same bot")
        print("      token while mkwlsoso-bot is running. Sending is safe, but if")
        print("      the panel starts missing button presses, stop the service")
        print("      first and run this again.")
        show("H_telegram_delivery", await send_pdf(pdf["pdf_path"]))
    else:
        show("H_telegram_delivery",
             {"ok": None, "skipped": "pass --send to test delivery"})

    # ----------------------- verdict -----------------------
    st = RESULT["steps"]
    print("-" * 70)
    print("  GAPS CLOSED?")
    print()
    checks = [
        ("A  full photo census", (st.get("A_census") or {}).get("ok")),
        ("B  photo paging inside a chat",
         (st.get("B_photo_paging") or {}).get("paging_works")),
        ("C  file_reference stable / reusable",
         (st.get("C_reference") or {}).get("redownload_with_original_ref_ok")),
        ("D  chunked download", (st.get("D_chunked") or {}).get("ok")),
        ("E  sustained concurrency, no flood",
         (st.get("E_sustained") or {}).get("ok")),
        ("F  base64 batch transfer", (st.get("F_b64_transfer") or {}).get("ok")),
        ("G  PDF generation", (st.get("G_pdf_render") or {}).get("ok")),
        ("H  Telegram delivery", (st.get("H_telegram_delivery") or {}).get("ok")),
        ("I  photos-as-documents checked",
         (st.get("I_image_documents") or {}).get("ok")),
    ]
    for label, ok in checks:
        mark = "OK  " if ok else ("SKIP" if ok is None else "NO  ")
        print(f"    [{mark}] {label}")
    print()

    cen = st.get("A_census") or {}
    if cen.get("ok"):
        print(f"    chats examined      : {cen.get('examined')}")
        print(f"    chats with photos   : {cen.get('chats_with_photos')}")
        print(f"    photos total        : {cen.get('photos_total_declared')}")
        print(f"    sent by me          : {cen.get('sent_by_me')}")
        print(f"    scan rate           : {cen.get('ms_per_chat')} ms/chat")
    su = st.get("E_sustained") or {}
    if su.get("ok"):
        print(f"    sustained download  : {su.get('ms_per_photo')} ms/photo "
              f"at conc {su.get('concurrency')}, {su.get('throughput_kb_s')} KB/s")
    g = st.get("G_pdf_render") or {}
    if g.get("ok"):
        print(f"    pdf                 : {g.get('images')} images -> "
              f"{g.get('pdf_kb')} KB in {g.get('render_ms')} ms "
              f"({g.get('ms_per_image')} ms/image)")
    ic = st.get("I_image_documents") or {}
    if ic.get("ok"):
        print(f"    image documents     : {ic.get('image_mime_documents')} "
              f"(photos sent as files -- missed by the photo filter)")
    print()
    print(f"  full JSON: {OUT}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not argv:
        print(f"usage: {sys.argv[0]} <account> [--quick] [--send]")
        sys.exit(1)
    try:
        sys.exit(asyncio.run(main(argv[0], "--quick" in sys.argv,
                                  "--send" in sys.argv)))
    except KeyboardInterrupt:
        print("\n  interrupted -- partial results kept:")
        for k in RESULT["steps"]:
            print(f"     {k}")
        save()
        print(f"  saved: {OUT}")
        sys.exit(130)
