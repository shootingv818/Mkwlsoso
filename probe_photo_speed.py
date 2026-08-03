#!/usr/bin/env python3
"""
PHOTO EXPORT PROBE -- ROUND 4: find the FASTEST possible path.

Round 3 proved the feature is buildable:
    upload.getFile works, full quality 1080px in 832ms (no dc opts),
    thumbnail 83x90 in 279ms, messages.search + photo filter works,
    pFlags.out separates sent from received, searchGlobal is unavailable.

Quality requirement is now relaxed: "clear enough", not 4K. So this round stops
asking *can we* and measures *how fast*, across every route worth trying:

  A  inline / stripped bytes         -- photos may already carry small bytes in
                                       the message, which would need ZERO
                                       download calls
  B  getPreviewURLFromBytes          -- tweb's own instant-preview path
                                       (this method exists on appPhotosManager)
  C  size ladder timing              -- s vs m vs x, real milliseconds
  D  download concurrency 1/4/8/16   -- does tweb parallelise, or queue?
                                       THE number that decides the engine
  E  search concurrency 1/4/8        -- same question for the search phase
  F  full dialog paging              -- the REAL chat count, timed
  G  photos per chat with limit 100  -- round 2 used limit 10 and undercounted
  H  searchGlobal variants           -- one more attempt to unlock the
                                       single-call path
  I  getSearchCounters               -- cheap way to skip chats with no photos
  J  repeat the same photo           -- is there a client-side cache?
  K  base64 transfer batching        -- measured from Python: how big a batch
                                       can cross the bridge before it hurts

READ-ONLY. Sends nothing, writes nothing to Eitaa, changes no bot state.
FLOOD_WAIT is caught and reported (that answer is useful too).

Usage:
    cd ~/Mkwlsoso
    .venv/bin/python probe_photo_speed.py 989124089268
"""

from __future__ import annotations

import asyncio
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

# ---------------------------------------------------------------------------
# PHASE 1: discovery + all in-page measurements
# ---------------------------------------------------------------------------
DISCOVER_JS = r"""
async (cfg) => {
  const AM = window.apiManager;
  const out = { ok: false, probes: {}, errors: [], pool: [] };
  const S = v => { try { return String(v); } catch (e) { return null; } };
  const now = () => performance.now();
  function err(label, e) {
    const s = String((e && (e.type || e.error_message || e.code || e.message)) || e);
    out.errors.push(label + ': ' + s);
    return s;
  }
  const isFlood = s => /FLOOD|TOO_MANY|LIMIT/i.test(String(s || ''));

  if (!AM || !AM.invokeApi) { out.errors.push('FATAL: no apiManager'); return out; }
  out.ok = true;

  // ===================== F: full dialog paging =====================
  const peers = [];          // {peer, name, user_id}
  try {
    const t0 = now();
    let offset_date = 0, offset_id = 0, offset_peer = { _: 'inputPeerEmpty' };
    let pages = 0, totalDialogs = 0, users_seen = 0;
    for (let loop = 0; loop < cfg.maxDialogPages; loop++) {
      const d = await AM.invokeApi('messages.getDialogs', {
        folder_id: 0, offset_date, offset_id, offset_peer, limit: 100, hash: 0 });
      const dialogs = (d && d.dialogs) || [];
      const users = (d && d.users) || [];
      const chats = (d && d.chats) || [];
      const messages = (d && d.messages) || [];
      pages++; totalDialogs += dialogs.length; users_seen += users.length;
      for (const dlg of dialogs) {
        if (!dlg.peer || dlg.peer._ !== 'peerUser') continue;
        const u = users.find(x => S(x.id) === S(dlg.peer.user_id));
        if (!u || !u.access_hash) continue;
        peers.push({
          peer: { _: 'inputPeerUser', user_id: +u.id, access_hash: u.access_hash },
          name: ((u.first_name || '') + ' ' + (u.last_name || '')).trim() || S(u.id),
          user_id: S(u.id),
        });
      }
      if (dialogs.length < 100) break;
      const last = dialogs[dialogs.length - 1];
      const topId = last.top_message || 0;
      const lastMsg = messages.find(m => m.id === topId);
      offset_id = topId;
      offset_date = lastMsg ? lastMsg.date : offset_date;
      // rebuild offset_peer
      const lp = last.peer;
      if (lp._ === 'peerUser') {
        const u = users.find(x => S(x.id) === S(lp.user_id));
        offset_peer = u ? { _: 'inputPeerUser', user_id: +u.id, access_hash: u.access_hash }
                        : { _: 'inputPeerEmpty' };
      } else if (lp._ === 'peerChannel') {
        const c = chats.find(x => S(x.id) === S(lp.channel_id));
        offset_peer = c ? { _: 'inputPeerChannel', channel_id: +c.id, access_hash: c.access_hash }
                        : { _: 'inputPeerEmpty' };
      } else if (lp._ === 'peerChat') {
        offset_peer = { _: 'inputPeerChat', chat_id: lp.chat_id };
      } else { break; }
      if (offset_peer._ === 'inputPeerEmpty') break;
    }
    out.probes.F_dialogs = {
      ok: true, pages, total_dialogs: totalDialogs, pv_peers: peers.length,
      ms: Math.round(now() - t0),
      ms_per_page: pages ? Math.round((now() - t0) / pages) : null,
    };
  } catch (e) { out.probes.F_dialogs = { ok: false, error: err('F', e) }; }

  // ===================== H: searchGlobal variants =====================
  const sgTries = [];
  const sgVariants = [
    ['minimal', { q: '', filter: { _: 'inputMessagesFilterPhotos' }, min_date: 0,
                  max_date: 0, offset_rate: 0,
                  offset_peer: { _: 'inputPeerEmpty' }, offset_id: 0, limit: 20 }],
    ['with_flags', { flags: 0, q: '', filter: { _: 'inputMessagesFilterPhotos' },
                     min_date: 0, max_date: 0, offset_rate: 0,
                     offset_peer: { _: 'inputPeerEmpty' }, offset_id: 0, limit: 20 }],
    ['folder', { flags: 1, folder_id: 0, q: '',
                 filter: { _: 'inputMessagesFilterPhotos' }, min_date: 0, max_date: 0,
                 offset_rate: 0, offset_peer: { _: 'inputPeerEmpty' },
                 offset_id: 0, limit: 20 }],
    ['no_filter', { q: 'a', min_date: 0, max_date: 0, offset_rate: 0,
                    offset_peer: { _: 'inputPeerEmpty' }, offset_id: 0, limit: 5 }],
  ];
  for (const [name, params] of sgVariants) {
    try {
      const t = now();
      const r = await AM.invokeApi('messages.searchGlobal', params);
      const msgs = (r && r.messages) || [];
      sgTries.push({ name, ok: true, count: msgs.length, ms: Math.round(now() - t),
                     with_photo: msgs.filter(m => m.media && m.media.photo).length });
      break;
    } catch (e) { sgTries.push({ name, ok: false, error: err('H_' + name, e) }); }
  }
  out.probes.H_searchGlobal = { ok: sgTries.some(t => t.ok), tries: sgTries };

  // ===================== I: getSearchCounters =====================
  if (peers.length) {
    try {
      const t = now();
      const r = await AM.invokeApi('messages.getSearchCounters', {
        peer: peers[0].peer, filters: [{ _: 'inputMessagesFilterPhotos' }] });
      out.probes.I_searchCounters = {
        ok: true, ms: Math.round(now() - t),
        result: JSON.stringify(r).slice(0, 200),
      };
    } catch (e) { out.probes.I_searchCounters = { ok: false, error: err('I', e) }; }
  }

  // ===================== G + E: search with limit 100, paged, timed =====
  // Sequential over the first K peers to get a clean per-chat number.
  const photoPool = [];      // raw photo objects for the download tests
  let seqMs = 0, seqChats = 0, seqPhotos = 0, pagedChats = 0;
  try {
    const K = Math.min(peers.length, cfg.seqSearchChats);
    const t0 = now();
    for (let i = 0; i < K; i++) {
      let offset_id = 0, got = 0, pagesForChat = 0;
      for (let pg = 0; pg < cfg.maxPhotoPages; pg++) {
        const s = await AM.invokeApi('messages.search', {
          peer: peers[i].peer, q: '', filter: { _: 'inputMessagesFilterPhotos' },
          min_date: 0, max_date: 0, offset_id, add_offset: 0,
          limit: 100, max_id: 0, min_id: 0, hash: 0 });
        const msgs = (s && s.messages) || [];
        pagesForChat++;
        for (const m of msgs) {
          if (m.media && m.media.photo) {
            got++;
            if (photoPool.length < cfg.poolSize) {
              photoPool.push({ photo: m.media.photo, out: !!(m.pFlags && m.pFlags.out),
                               msg_id: m.id, chat: peers[i].name });
            }
          }
        }
        if (msgs.length < 100) break;
        offset_id = msgs[msgs.length - 1].id;
      }
      if (pagesForChat > 1) pagedChats++;
      seqPhotos += got; seqChats++;
    }
    seqMs = Math.round(now() - t0);
    out.probes.G_search_seq = {
      ok: true, chats: seqChats, photos_found: seqPhotos,
      total_ms: seqMs, ms_per_chat: seqChats ? Math.round(seqMs / seqChats) : null,
      chats_needing_more_than_one_page: pagedChats,
      pool_collected: photoPool.length,
    };
  } catch (e) { out.probes.G_search_seq = { ok: false, error: err('G', e) }; }

  // ===================== E: search concurrency ladder =====================
  const searchLadder = [];
  try {
    const start = cfg.seqSearchChats;
    for (const conc of cfg.searchConc) {
      const slice = peers.slice(start, start + conc);
      if (slice.length < conc) break;
      const t = now();
      const rs = await Promise.all(slice.map(p =>
        AM.invokeApi('messages.search', {
          peer: p.peer, q: '', filter: { _: 'inputMessagesFilterPhotos' },
          min_date: 0, max_date: 0, offset_id: 0, add_offset: 0,
          limit: 100, max_id: 0, min_id: 0, hash: 0 })
          .then(r => ((r && r.messages) || []).length)
          .catch(e => 'ERR:' + String(e && e.type || e).slice(0, 30))));
      const ms = Math.round(now() - t);
      searchLadder.push({ concurrency: conc, total_ms: ms,
                          ms_per_chat: Math.round(ms / conc),
                          results: rs.slice(0, 4),
                          errors: rs.filter(x => typeof x === 'string').length });
    }
    out.probes.E_search_conc = { ok: searchLadder.length > 0, ladder: searchLadder };
  } catch (e) { out.probes.E_search_conc = { ok: false, error: err('E', e) }; }

  if (!photoPool.length) {
    out.errors.push('no photos collected; download tests skipped');
    return out;
  }

  // ===================== A: inline / stripped bytes =====================
  try {
    const inline = [];
    for (const item of photoPool.slice(0, 5)) {
      for (const sz of (item.photo.sizes || [])) {
        const b = sz.bytes;
        if (b) inline.push({ ctor: sz._, type: sz.type, w: sz.w, h: sz.h,
                             bytes: b.length || b.byteLength || 0 });
      }
    }
    const ctors = new Set();
    for (const item of photoPool) for (const sz of (item.photo.sizes || [])) ctors.add(sz._);
    out.probes.A_inline_bytes = {
      ok: inline.length > 0, found: inline.slice(0, 8),
      size_constructors_seen: [...ctors],
    };
  } catch (e) { out.probes.A_inline_bytes = { ok: false, error: err('A', e) }; }

  // ===================== B: tweb instant preview =====================
  try {
    const APM = window.appPhotosManager;
    const tries = [];
    const p0 = photoPool[0].photo;
    const cands = [
      ['getPreviewURLFromBytes', () => {
        const sz = (p0.sizes || []).find(s => s.bytes);
        return sz ? APM.getPreviewURLFromBytes(sz.bytes) : null; }],
      ['getPreviewURLFromEmptyThumb', () => APM.getPreviewURLFromEmptyThumb(p0)],
      ['choosePhotoSize_m', () => JSON.stringify(APM.choosePhotoSize(p0, 320, 320))],
    ];
    for (const [name, fn] of cands) {
      try {
        const t = now();
        let r = fn();
        if (r && typeof r.then === 'function') r = await r;
        let bytes = 0;
        if (typeof r === 'string' && /^(blob:|data:|https?:)/.test(r)) {
          const resp = await fetch(r); bytes = (await resp.arrayBuffer()).byteLength;
        }
        tries.push({ name, ok: !!r, ms: Math.round(now() - t), bytes,
                     sample: r ? String(r).slice(0, 70) : null });
      } catch (e) { tries.push({ name, ok: false, error: err('B_' + name, e) }); }
    }
    out.probes.B_tweb_preview = { ok: tries.some(t => t.ok && t.bytes > 500), tries };
  } catch (e) { out.probes.B_tweb_preview = { ok: false, error: err('B', e) }; }

  // ===================== helper: one download =====================
  async function grab(photo, type) {
    const r = await AM.invokeApi('upload.getFile', {
      location: { _: 'inputPhotoFileLocation', id: photo.id,
                  access_hash: photo.access_hash,
                  file_reference: photo.file_reference, thumb_size: type },
      offset: 0, limit: cfg.getFileLimit });
    return r && r.bytes ? (r.bytes.length || r.bytes.byteLength || 0) : 0;
  }
  function sizeTypes(photo) {
    return (photo.sizes || []).filter(s => s.type).map(s => ({
      type: s.type, w: s.w || 0, size: s.size || 0 }));
  }

  // ===================== C: size ladder timing =====================
  try {
    const rows = [];
    const p0 = photoPool[0].photo;
    for (const s of sizeTypes(p0)) {
      try {
        const t = now();
        const b = await grab(p0, s.type);
        rows.push({ type: s.type, w: s.w, declared: s.size, bytes: b,
                    ms: Math.round(now() - t) });
      } catch (e) {
        rows.push({ type: s.type, w: s.w, ok: false,
                    error: err('C_' + s.type, e) });
      }
    }
    out.probes.C_size_timing = { ok: rows.some(r => r.bytes > 100), rows };
  } catch (e) { out.probes.C_size_timing = { ok: false, error: err('C', e) }; }

  // Pick the size the engine would actually use: closest to ~300px wide.
  let chosen = 'm';
  try {
    const cand = sizeTypes(photoPool[0].photo)
      .filter(s => s.w > 0)
      .sort((a, b) => Math.abs(a.w - cfg.targetWidth) - Math.abs(b.w - cfg.targetWidth));
    if (cand.length) chosen = cand[0].type;
  } catch (e) {}
  out.chosen_size = chosen;

  // ===================== J: repeat the same photo (cache?) =====================
  try {
    const p0 = photoPool[0].photo;
    const t1 = now(); const b1 = await grab(p0, chosen); const ms1 = Math.round(now() - t1);
    const t2 = now(); const b2 = await grab(p0, chosen); const ms2 = Math.round(now() - t2);
    const t3 = now(); const b3 = await grab(p0, chosen); const ms3 = Math.round(now() - t3);
    out.probes.J_repeat = { ok: true, bytes: b1, first_ms: ms1, second_ms: ms2,
                            third_ms: ms3,
                            cached: ms2 < ms1 * 0.5 || ms3 < ms1 * 0.5 };
  } catch (e) { out.probes.J_repeat = { ok: false, error: err('J', e) }; }

  // ===================== D: download concurrency ladder =====================
  const dlLadder = [];
  let cursor = 1;              // photo 0 was used by C/J
  try {
    for (const conc of cfg.dlConc) {
      const slice = photoPool.slice(cursor, cursor + conc);
      if (slice.length < conc) break;
      cursor += conc;
      const t = now();
      const rs = await Promise.all(slice.map(it =>
        grab(it.photo, chosen).catch(e => 'ERR:' + String(e && e.type || e).slice(0, 40))));
      const ms = Math.round(now() - t);
      const okCount = rs.filter(x => typeof x === 'number' && x > 100).length;
      const floods = rs.filter(x => typeof x === 'string' && isFlood(x)).length;
      const bytes = rs.filter(x => typeof x === 'number').reduce((a, b) => a + b, 0);
      dlLadder.push({ concurrency: conc, total_ms: ms,
                      ms_per_photo: Math.round(ms / conc),
                      ok: okCount, floods,
                      errors: rs.filter(x => typeof x === 'string').slice(0, 2),
                      total_bytes: bytes,
                      throughput_kb_s: ms ? Math.round(bytes / 1024 / (ms / 1000)) : 0 });
      if (floods) break;       // stop climbing once the server pushes back
    }
    out.probes.D_dl_conc = { ok: dlLadder.length > 0, size_used: chosen,
                             ladder: dlLadder };
  } catch (e) { out.probes.D_dl_conc = { ok: false, error: err('D', e) }; }

  // Keep a few photos addressable for the Python-side batching test.
  out.pool = photoPool.slice(0, cfg.poolSize).map(it => ({
    id: S(it.photo.id), access_hash: S(it.photo.access_hash),
    out: it.out, chat: it.chat,
    types: sizeTypes(it.photo).map(s => s.type),
  }));
  // Stash the raw pool on window so the batching probe can reuse it without
  // searching again.
  window.__MKWL_probe_pool = photoPool;
  window.__MKWL_probe_size = chosen;
  return out;
}
"""

# ---------------------------------------------------------------------------
# PHASE 2: how much data can cross the bridge at once (measured from Python)
# ---------------------------------------------------------------------------
BATCH_JS = r"""
async (n) => {
  const AM = window.apiManager;
  const pool = window.__MKWL_probe_pool || [];
  const type = window.__MKWL_probe_size || 'm';
  if (!pool.length) return { ok: false, error: 'no pool' };
  const t0 = performance.now();
  const slice = pool.slice(0, n);
  const results = await Promise.all(slice.map(async (it) => {
    try {
      const r = await AM.invokeApi('upload.getFile', {
        location: { _: 'inputPhotoFileLocation', id: it.photo.id,
                    access_hash: it.photo.access_hash,
                    file_reference: it.photo.file_reference, thumb_size: type },
        offset: 0, limit: 1048576 });
      const u8 = r && r.bytes ? new Uint8Array(r.bytes) : new Uint8Array(0);
      // base64 without blowing the stack on big buffers
      let s = '';
      const CH = 8192;
      for (let i = 0; i < u8.length; i += CH)
        s += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
      return btoa(s);
    } catch (e) { return ''; }
  }));
  const fetch_ms = Math.round(performance.now() - t0);
  const total_b64 = results.reduce((a, b) => a + b.length, 0);
  return { ok: true, n: slice.length, fetch_ms, total_b64,
           images: results.map(r => r.length) };
}
"""

CFG = {
    "maxDialogPages": 12,
    "seqSearchChats": 5,
    "maxPhotoPages": 5,
    "poolSize": 64,
    "searchConc": [2, 4, 8],
    "dlConc": [1, 4, 8, 16],
    "getFileLimit": 1048576,
    "targetWidth": 320,
}


def _p(label, obj, width=250):
    print(f"  {label}: {json.dumps(obj, ensure_ascii=False, default=str)[:width]}")


async def main(account: str) -> int:
    print("=" * 70)
    print(f"  PHOTO SPEED PROBE (round 4) -- {account}")
    print("=" * 70)
    print()

    t0 = time.time()
    async with session_pool.lease(account, headed=config.HEADED_JOBS) as session:
        driver = EitaaDriver(session)
        await driver.open()
        print(f"  session ready in {time.time() - t0:.1f}s")
        if not await driver.is_logged_in():
            print("  FATAL: not logged in")
            return 1
        print("  running measurements (this can take 2-3 minutes)...")
        print()

        t_disc = time.time()
        try:
            res = await driver.page.evaluate(DISCOVER_JS, CFG)
        except Exception as exc:
            print(f"  discovery failed: {type(exc).__name__}: {exc}")
            return 1
        disc_s = time.time() - t_disc
        print(f"  discovery phase done in {disc_s:.1f}s")
        print()

        if not res or not res.get("ok"):
            print(f"  bridge reported failure: {res}")
            return 1

        probes = res.get("probes") or {}

        for key in ["F_dialogs", "H_searchGlobal", "I_searchCounters",
                    "G_search_seq", "E_search_conc", "A_inline_bytes",
                    "B_tweb_preview", "C_size_timing", "J_repeat", "D_dl_conc"]:
            p = probes.get(key)
            if p is None:
                continue
            print(f"  [{'OK  ' if p.get('ok') else 'FAIL'}] {key}")
            for k, v in p.items():
                if k == "ok":
                    continue
                if isinstance(v, list):
                    print(f"        {k}:")
                    for item in v[:8]:
                        print(f"          {json.dumps(item, ensure_ascii=False, default=str)[:200]}")
                else:
                    _p(f"      {k}", v, 200)
            print()

        # ---- K: batching across the bridge -----------------------------
        print("  [....] K_batch_transfer (measured from Python)")
        batch_rows = []
        for n in (1, 4, 8, 16, 32):
            try:
                t = time.time()
                r = await driver.page.evaluate(BATCH_JS, n)
                wall = time.time() - t
                if not r or not r.get("ok"):
                    print(f"        n={n}: {r}")
                    break
                b64 = r.get("total_b64", 0)
                row = {
                    "n": r.get("n"),
                    "in_page_fetch_ms": r.get("fetch_ms"),
                    "wall_ms": round(wall * 1000),
                    "bridge_overhead_ms": round(wall * 1000) - (r.get("fetch_ms") or 0),
                    "total_b64_kb": round(b64 / 1024),
                    "per_photo_ms": round(wall * 1000 / max(1, r.get("n") or 1)),
                }
                batch_rows.append(row)
                print(f"        {json.dumps(row)}")
            except Exception as exc:
                print(f"        n={n} failed: {type(exc).__name__}: {exc}")
                break
        print()

    if res.get("errors"):
        print("  errors:")
        for e in res["errors"]:
            print(f"     {e}")
        print()

    # ---------------- projection ----------------
    print("-" * 70)
    print("  PROJECTION")
    print()
    F = probes.get("F_dialogs") or {}
    G = probes.get("G_search_seq") or {}
    D = probes.get("D_dl_conc") or {}
    E = probes.get("E_search_conc") or {}

    chats = F.get("pv_peers") or 0
    per_chat = G.get("ms_per_chat") or 0
    photos_per_chat = ((G.get("photos_found") or 0) / (G.get("chats") or 1))
    est_photos = int(photos_per_chat * chats)

    print(f"    PV chats found            : {chats}")
    print(f"    photos per chat (sampled) : {photos_per_chat:.1f}")
    print(f"    estimated total photos    : {est_photos}")
    print(f"    size the engine would use : {res.get('chosen_size')}")
    print()

    ladder = D.get("ladder") or []
    best_dl = None
    for row in ladder:
        if row.get("floods"):
            continue
        if best_dl is None or row["ms_per_photo"] < best_dl["ms_per_photo"]:
            best_dl = row
    sladder = E.get("ladder") or []
    best_s = None
    for row in sladder:
        if best_s is None or row["ms_per_chat"] < best_s["ms_per_chat"]:
            best_s = row

    if best_dl:
        print(f"    fastest download setting  : concurrency {best_dl['concurrency']} "
              f"-> {best_dl['ms_per_photo']} ms/photo "
              f"({best_dl.get('throughput_kb_s')} KB/s)")
    if best_s:
        print(f"    fastest search setting    : concurrency {best_s['concurrency']} "
              f"-> {best_s['ms_per_chat']} ms/chat")
    print()

    if best_dl and best_s and chats:
        search_s = chats * best_s["ms_per_chat"] / 1000
        dl_s = est_photos * best_dl["ms_per_photo"] / 1000
        seq_search = chats * (per_chat or 1900) / 1000
        seq_dl = est_photos * 0.83
        print(f"    sequential estimate       : {seq_search + seq_dl:.0f}s "
              f"({(seq_search + seq_dl) / 60:.1f} min)")
        print(f"    tuned estimate            : {search_s + dl_s:.0f}s "
              f"({(search_s + dl_s) / 60:.1f} min)")
        if seq_search + seq_dl > 0:
            print(f"    speedup                   : "
                  f"{(seq_search + seq_dl) / max(1e-9, search_s + dl_s):.1f}x")
    print()

    zero_dl = (probes.get("A_inline_bytes") or {}).get("ok") or \
              (probes.get("B_tweb_preview") or {}).get("ok")
    print(f"    zero-download path exists : {bool(zero_dl)}")
    print(f"    searchGlobal usable       : "
          f"{bool((probes.get('H_searchGlobal') or {}).get('ok'))}")
    print(f"    getSearchCounters usable  : "
          f"{bool((probes.get('I_searchCounters') or {}).get('ok'))}")
    print(f"    client-side cache         : "
          f"{bool((probes.get('J_repeat') or {}).get('cached'))}")
    print()

    payload = {"discovery": res, "batch": batch_rows}
    out_path = Path("/tmp/photo_probe4_result.json")
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"  full JSON: {out_path}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <account>")
        sys.exit(1)
    try:
        sys.exit(asyncio.run(main(sys.argv[1])))
    except KeyboardInterrupt:
        sys.exit(130)
