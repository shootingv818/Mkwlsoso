#!/usr/bin/env python3
"""
PHOTO EXPORT PROBE -- ROUND 5: bounded, streaming, and it answers the
"how do we skip chats with no photos" question.

Why this replaces round 4:
  Round 4 put every measurement inside ONE page.evaluate(), so nothing printed
  until the whole thing finished and Ctrl+C threw all of it away. It also let
  itself walk far more dialogs than a probe needs. This version runs each step
  as its own short call, prints the result immediately, saves partial JSON after
  every step, and stops at hard caps.

New measurement that matters most for speed:
  messages.search returns a `count` field -- the TOTAL number of photos in that
  chat. So one cheap call per chat tells us whether to bother with it at all,
  and exactly how many pages it needs. This is the "skip the empty chats" answer.

Every step has a cap and a timeout. Read-only throughout: getDialogs, search,
getFile. Nothing is sent, nothing is written to Eitaa, no bot state changes.

Usage:
    cd ~/Mkwlsoso
    .venv/bin/python probe_speed2.py 989124089268

Everything is bounded to roughly 60-90 seconds. Ctrl+C is safe: whatever has
been measured so far is printed and saved.
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

OUT = Path("/tmp/photo_probe5_result.json")
RESULT: dict = {"steps": {}}

# ---- caps: a probe samples, it does not do the real job --------------------
MAX_DIALOG_PAGES = 2        # 200 dialogs is plenty to characterise an account
SAMPLE_CHATS = 6            # for the cheap-count measurement
DL_CONC = [1, 4, 8, 16]     # download concurrency ladder
SEARCH_CONC = [4, 8]        # search concurrency ladder
STEP_TIMEOUT = 45           # seconds per step


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
            if isinstance(v, list):
                print(f"      {k}:")
                for item in v[:8]:
                    print(f"        {json.dumps(item, ensure_ascii=False, default=str)[:190]}")
            else:
                print(f"      {k}: {json.dumps(v, ensure_ascii=False, default=str)[:190]}")
    else:
        print(f"      {json.dumps(data, ensure_ascii=False, default=str)[:400]}")
    print()


# ---------------------------------------------------------------------------
# JS_1: list PV peers (bounded paging)
# ---------------------------------------------------------------------------
JS_DIALOGS = r"""
async (maxPages) => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, error: 'no apiManager' };
  const S = v => String(v);
  const t0 = performance.now();
  let offset_date = 0, offset_id = 0, offset_peer = { _: 'inputPeerEmpty' };
  let pages = 0, total = 0, more = false;
  const peers = [];
  try {
    for (let i = 0; i < maxPages; i++) {
      const d = await AM.invokeApi('messages.getDialogs', {
        folder_id: 0, offset_date, offset_id, offset_peer, limit: 100, hash: 0 });
      const dialogs = (d && d.dialogs) || [], users = (d && d.users) || [],
            chats = (d && d.chats) || [], messages = (d && d.messages) || [];
      pages++; total += dialogs.length;
      for (const dlg of dialogs) {
        if (!dlg.peer || dlg.peer._ !== 'peerUser') continue;
        const u = users.find(x => S(x.id) === S(dlg.peer.user_id));
        if (!u || !u.access_hash) continue;
        peers.push({ user_id: S(u.id), access_hash: S(u.access_hash),
                     name: ((u.first_name||'')+' '+(u.last_name||'')).trim() || S(u.id),
                     top_message: dlg.top_message || 0 });
      }
      if (dialogs.length < 100) { more = false; break; }
      more = true;
      const last = dialogs[dialogs.length - 1];
      const topId = last.top_message || 0;
      const lm = messages.find(m => m.id === topId);
      offset_id = topId; offset_date = lm ? lm.date : offset_date;
      const lp = last.peer;
      if (lp._ === 'peerUser') {
        const u = users.find(x => S(x.id) === S(lp.user_id));
        offset_peer = u ? { _:'inputPeerUser', user_id:+u.id, access_hash:u.access_hash }
                        : { _:'inputPeerEmpty' };
      } else if (lp._ === 'peerChannel') {
        const c = chats.find(x => S(x.id) === S(lp.channel_id));
        offset_peer = c ? { _:'inputPeerChannel', channel_id:+c.id, access_hash:c.access_hash }
                        : { _:'inputPeerEmpty' };
      } else if (lp._ === 'peerChat') {
        offset_peer = { _:'inputPeerChat', chat_id: lp.chat_id };
      } else break;
      if (offset_peer._ === 'inputPeerEmpty') break;
    }
  } catch (e) {
    return { ok: false, error: String(e && (e.type||e.message) || e), peers, pages };
  }
  window.__MKWL_peers = peers;
  return { ok: true, pages, dialogs_seen: total, pv_peers: peers.length,
           more_pages_exist: more, ms: Math.round(performance.now() - t0),
           ms_per_page: pages ? Math.round((performance.now()-t0)/pages) : null };
}
"""

# ---------------------------------------------------------------------------
# JS_2: the cheap count probe -- does search return `count`?
# ---------------------------------------------------------------------------
JS_COUNT = r"""
async (n) => {
  const AM = window.apiManager;
  const peers = window.__MKWL_peers || [];
  if (!peers.length) return { ok: false, error: 'no peers' };
  const rows = [];
  const slice = peers.slice(0, n);
  for (const p of slice) {
    const peer = { _:'inputPeerUser', user_id:+p.user_id, access_hash:p.access_hash };
    try {
      const t = performance.now();
      const r = await AM.invokeApi('messages.search', {
        peer, q: '', filter: { _: 'inputMessagesFilterPhotos' },
        min_date: 0, max_date: 0, offset_id: 0, add_offset: 0,
        limit: 1, max_id: 0, min_id: 0, hash: 0 });
      rows.push({ chat: p.name.slice(0, 22),
                  result_type: r ? r._ : null,
                  count_field: (r && r.count != null) ? r.count : null,
                  returned: ((r && r.messages) || []).length,
                  ms: Math.round(performance.now() - t) });
    } catch (e) {
      rows.push({ chat: p.name.slice(0, 22), error: String(e && (e.type||e.message) || e) });
    }
  }
  const withCount = rows.filter(r => r.count_field != null);
  return { ok: withCount.length > 0, rows,
           count_field_available: withCount.length > 0,
           chats_with_zero_photos: rows.filter(r => r.count_field === 0).length,
           chats_with_photos: rows.filter(r => r.count_field > 0).length,
           avg_ms: rows.length ? Math.round(
             rows.filter(r=>r.ms).reduce((a,b)=>a+b.ms,0) / Math.max(1,rows.filter(r=>r.ms).length)) : null };
}
"""

# ---------------------------------------------------------------------------
# JS_3: full search (limit 100) on chats known to have photos + build pool
# ---------------------------------------------------------------------------
JS_SEARCH = r"""
async (n) => {
  const AM = window.apiManager;
  const peers = window.__MKWL_peers || [];
  const pool = [];
  const rows = [];
  let scanned = 0;
  for (const p of peers) {
    if (rows.length >= n) break;
    const peer = { _:'inputPeerUser', user_id:+p.user_id, access_hash:p.access_hash };
    scanned++;
    if (scanned > 40) break;
    try {
      const t = performance.now();
      const r = await AM.invokeApi('messages.search', {
        peer, q: '', filter: { _: 'inputMessagesFilterPhotos' },
        min_date: 0, max_date: 0, offset_id: 0, add_offset: 0,
        limit: 100, max_id: 0, min_id: 0, hash: 0 });
      const msgs = (r && r.messages) || [];
      const photos = msgs.filter(m => m.media && m.media.photo);
      if (!photos.length) continue;
      for (const m of photos) {
        if (pool.length < 48)
          pool.push({ photo: m.media.photo, out: !!(m.pFlags && m.pFlags.out) });
      }
      rows.push({ chat: p.name.slice(0, 22), total_count: r.count != null ? r.count : null,
                  returned: msgs.length, photos: photos.length,
                  sent_by_me: photos.filter(m => m.pFlags && m.pFlags.out).length,
                  ms: Math.round(performance.now() - t) });
    } catch (e) {
      rows.push({ chat: p.name.slice(0, 22), error: String(e && (e.type||e.message) || e) });
    }
  }
  window.__MKWL_pool = pool;
  // pick the size nearest 320px wide
  let chosen = 'm';
  if (pool.length) {
    const cand = (pool[0].photo.sizes || []).filter(s => s.type && s.w)
      .sort((a,b) => Math.abs(a.w-320) - Math.abs(b.w-320));
    if (cand.length) chosen = cand[0].type;
  }
  window.__MKWL_size = chosen;
  return { ok: pool.length > 0, chats_scanned: scanned, chats_with_photos: rows.length,
           rows, pool_size: pool.length, chosen_size: chosen,
           sizes_available: pool.length ? (pool[0].photo.sizes||[]).map(s =>
             ({ type: s.type, w: s.w, h: s.h, bytes: s.size, ctor: s._ })) : [] };
}
"""

# ---------------------------------------------------------------------------
# JS_4: inline bytes (zero-download path)
# ---------------------------------------------------------------------------
JS_INLINE = r"""
async () => {
  const pool = window.__MKWL_pool || [];
  if (!pool.length) return { ok: false, error: 'no pool' };
  const ctors = new Set(); const inline = [];
  for (const it of pool) {
    for (const s of (it.photo.sizes || [])) {
      ctors.add(s._);
      const b = s.bytes;
      if (b && (b.length || b.byteLength))
        inline.push({ ctor: s._, type: s.type, w: s.w, h: s.h,
                      bytes: b.length || b.byteLength });
    }
  }
  let previewOk = false, previewBytes = 0;
  try {
    const APM = window.appPhotosManager;
    const sz = (pool[0].photo.sizes || []).find(s => s.bytes);
    if (APM && APM.getPreviewURLFromBytes && sz) {
      const u = APM.getPreviewURLFromBytes(sz.bytes);
      if (u) { const r = await fetch(u); previewBytes = (await r.arrayBuffer()).byteLength;
               previewOk = previewBytes > 200; }
    }
  } catch (e) {}
  return { ok: inline.length > 0, size_constructors: [...ctors],
           inline_found: inline.slice(0, 6),
           tweb_preview_ok: previewOk, tweb_preview_bytes: previewBytes };
}
"""

# ---------------------------------------------------------------------------
# JS_5: download concurrency ladder
# ---------------------------------------------------------------------------
JS_DL = r"""
async (concList) => {
  const AM = window.apiManager;
  const pool = window.__MKWL_pool || [];
  const type = window.__MKWL_size || 'm';
  if (!pool.length) return { ok: false, error: 'no pool' };
  const isFlood = s => /FLOOD|TOO_MANY|LIMIT/i.test(String(s||''));
  async function grab(photo) {
    const r = await AM.invokeApi('upload.getFile', {
      location: { _:'inputPhotoFileLocation', id: photo.id,
                  access_hash: photo.access_hash,
                  file_reference: photo.file_reference, thumb_size: type },
      offset: 0, limit: 1048576 });
    return r && r.bytes ? (r.bytes.length || r.bytes.byteLength || 0) : 0;
  }
  const ladder = []; let cur = 0;
  for (const conc of concList) {
    const slice = pool.slice(cur, cur + conc);
    if (slice.length < conc) break;
    cur += conc;
    const t = performance.now();
    const rs = await Promise.all(slice.map(it =>
      grab(it.photo).catch(e => 'ERR:' + String(e && (e.type||e.message) || e).slice(0,40))));
    const ms = Math.round(performance.now() - t);
    const nums = rs.filter(x => typeof x === 'number');
    const bytes = nums.reduce((a,b)=>a+b, 0);
    const floods = rs.filter(x => typeof x === 'string' && isFlood(x)).length;
    ladder.push({ concurrency: conc, total_ms: ms,
                  ms_per_photo: Math.round(ms / conc),
                  ok: nums.filter(b => b > 100).length, floods,
                  sample_error: rs.find(x => typeof x === 'string') || null,
                  kb: Math.round(bytes/1024),
                  throughput_kb_s: ms ? Math.round(bytes/1024/(ms/1000)) : 0 });
    if (floods) break;
  }
  return { ok: ladder.length > 0, size_used: type, ladder };
}
"""

# ---------------------------------------------------------------------------
# JS_6: search concurrency ladder
# ---------------------------------------------------------------------------
JS_SCONC = r"""
async (concList) => {
  const AM = window.apiManager;
  const peers = window.__MKWL_peers || [];
  if (peers.length < 8) return { ok: false, error: 'not enough peers' };
  const ladder = []; let cur = 0;
  for (const conc of concList) {
    const slice = peers.slice(cur, cur + conc);
    if (slice.length < conc) break;
    cur += conc;
    const t = performance.now();
    const rs = await Promise.all(slice.map(p =>
      AM.invokeApi('messages.search', {
        peer: { _:'inputPeerUser', user_id:+p.user_id, access_hash:p.access_hash },
        q: '', filter: { _:'inputMessagesFilterPhotos' }, min_date:0, max_date:0,
        offset_id:0, add_offset:0, limit:1, max_id:0, min_id:0, hash:0 })
      .then(r => (r && r.count != null) ? r.count : -1)
      .catch(e => 'ERR:' + String(e && (e.type||e.message) || e).slice(0,30))));
    const ms = Math.round(performance.now() - t);
    ladder.push({ concurrency: conc, total_ms: ms,
                  ms_per_chat: Math.round(ms / conc),
                  counts: rs.slice(0, 6),
                  errors: rs.filter(x => typeof x === 'string').length });
  }
  return { ok: ladder.length > 0, ladder };
}
"""

# ---------------------------------------------------------------------------
# JS_7: searchGlobal one more try
# ---------------------------------------------------------------------------
JS_GLOBAL = r"""
async () => {
  const AM = window.apiManager;
  const variants = [
    ['minimal', { q:'', filter:{_:'inputMessagesFilterPhotos'}, min_date:0, max_date:0,
                  offset_rate:0, offset_peer:{_:'inputPeerEmpty'}, offset_id:0, limit:10 }],
    ['flags0', { flags:0, q:'', filter:{_:'inputMessagesFilterPhotos'}, min_date:0,
                 max_date:0, offset_rate:0, offset_peer:{_:'inputPeerEmpty'},
                 offset_id:0, limit:10 }],
    ['folder0', { flags:1, folder_id:0, q:'', filter:{_:'inputMessagesFilterPhotos'},
                  min_date:0, max_date:0, offset_rate:0,
                  offset_peer:{_:'inputPeerEmpty'}, offset_id:0, limit:10 }],
  ];
  const tries = [];
  for (const [name, params] of variants) {
    try {
      const t = performance.now();
      const r = await AM.invokeApi('messages.searchGlobal', params);
      const msgs = (r && r.messages) || [];
      tries.push({ name, ok: true, count_field: r && r.count, returned: msgs.length,
                   with_photo: msgs.filter(m => m.media && m.media.photo).length,
                   ms: Math.round(performance.now() - t) });
      break;
    } catch (e) {
      tries.push({ name, ok: false, error: String(e && (e.type||e.message) || e).slice(0,60) });
    }
  }
  return { ok: tries.some(t => t.ok), tries };
}
"""


async def step(driver, label: str, js: str, arg=None):
    print(f"  ... {label}")
    t = time.time()
    try:
        coro = driver.page.evaluate(js, arg) if arg is not None \
            else driver.page.evaluate(js)
        res = await asyncio.wait_for(coro, timeout=STEP_TIMEOUT)
    except asyncio.TimeoutError:
        res = {"ok": False, "error": f"timed out after {STEP_TIMEOUT}s"}
    except Exception as exc:
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if isinstance(res, dict):
        res["_wall_s"] = round(time.time() - t, 1)
    show(label, res)
    return res


async def main(account: str) -> int:
    print("=" * 70)
    print(f"  PHOTO SPEED PROBE (round 5, bounded) -- {account}")
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
        print()

        d = await step(driver, "1_dialogs", JS_DIALOGS, MAX_DIALOG_PAGES)
        if not d.get("ok"):
            print("  cannot continue without peers")
            return 1

        await step(driver, "2_cheap_count", JS_COUNT, SAMPLE_CHATS)
        s = await step(driver, "3_search_full", JS_SEARCH, 6)
        await step(driver, "4_inline_bytes", JS_INLINE)
        if s.get("ok"):
            await step(driver, "5_download_conc", JS_DL, DL_CONC)
        await step(driver, "6_search_conc", JS_SCONC, SEARCH_CONC)
        await step(driver, "7_search_global", JS_GLOBAL)

    # ------------------ projection ------------------
    st = RESULT["steps"]
    print("-" * 70)
    print("  PROJECTION")
    print()

    dial = st.get("1_dialogs", {})
    cnt = st.get("2_cheap_count", {})
    srch = st.get("3_search_full", {})
    dl = st.get("5_download_conc", {})
    sc = st.get("6_search_conc", {})

    pv = dial.get("pv_peers") or 0
    more = dial.get("more_pages_exist")
    print(f"    PV chats seen             : {pv}"
          f"{'  (more pages exist -- real total is higher)' if more else ''}")

    zero = cnt.get("chats_with_zero_photos")
    withp = cnt.get("chats_with_photos")
    if zero is not None and withp is not None and (zero + withp):
        pct = 100.0 * zero / (zero + withp)
        print(f"    sampled chats with 0 photos: {zero} of {zero + withp} ({pct:.0f}%)")
        print(f"    -> skipping them saves that share of the heavy search")
    print(f"    count field available     : {cnt.get('count_field_available')}")
    print(f"    cheap count call          : {cnt.get('avg_ms')} ms/chat")

    rows = srch.get("rows") or []
    photos = sum(r.get("photos") or 0 for r in rows)
    totals = [r.get("total_count") for r in rows if r.get("total_count") is not None]
    print(f"    chats sampled with photos : {len(rows)}")
    print(f"    photos seen in sample     : {photos}")
    if totals:
        print(f"    per-chat totals reported  : {totals[:8]}")
    print(f"    size the engine would use : {srch.get('chosen_size')}")

    best_dl = None
    for r in (dl.get("ladder") or []):
        if r.get("floods"):
            continue
        if best_dl is None or r["ms_per_photo"] < best_dl["ms_per_photo"]:
            best_dl = r
    best_sc = None
    for r in (sc.get("ladder") or []):
        if best_sc is None or r["ms_per_chat"] < best_sc["ms_per_chat"]:
            best_sc = r
    if best_dl:
        print(f"    fastest download          : conc {best_dl['concurrency']} -> "
              f"{best_dl['ms_per_photo']} ms/photo, {best_dl.get('throughput_kb_s')} KB/s")
    if best_sc:
        print(f"    fastest count scan        : conc {best_sc['concurrency']} -> "
              f"{best_sc['ms_per_chat']} ms/chat")
    print(f"    zero-download path        : "
          f"{bool((st.get('4_inline_bytes') or {}).get('ok'))}")
    print(f"    tweb instant preview      : "
          f"{bool((st.get('4_inline_bytes') or {}).get('tweb_preview_ok'))}")
    print(f"    searchGlobal usable       : "
          f"{bool((st.get('7_search_global') or {}).get('ok'))}")
    print()

    if best_dl and best_sc and pv and rows:
        per_chat_photos = photos / max(1, len(rows))
        share_with = (withp / max(1, (zero or 0) + (withp or 0))) if withp is not None else 1.0
        est_photos = int(pv * share_with * per_chat_photos)
        scan_s = pv * best_sc["ms_per_chat"] / 1000
        heavy_s = pv * share_with * (best_sc["ms_per_chat"] / 1000)
        dl_s = est_photos * best_dl["ms_per_photo"] / 1000
        print(f"    estimated photos overall  : ~{est_photos}")
        print(f"    tuned run: scan {scan_s:.0f}s + fetch {heavy_s:.0f}s + "
              f"download {dl_s:.0f}s = {scan_s + heavy_s + dl_s:.0f}s "
              f"({(scan_s + heavy_s + dl_s)/60:.1f} min)")
        naive = pv * 1.9 + est_photos * 0.83
        print(f"    naive sequential          : {naive:.0f}s ({naive/60:.1f} min)")
        if scan_s + heavy_s + dl_s > 0:
            print(f"    speedup                   : "
                  f"{naive/(scan_s+heavy_s+dl_s):.1f}x")
    print()
    print(f"  full JSON: {OUT}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <account>")
        sys.exit(1)
    try:
        sys.exit(asyncio.run(main(sys.argv[1])))
    except KeyboardInterrupt:
        print("\n  interrupted -- partial results kept:")
        for k in RESULT["steps"]:
            print(f"     {k}")
        save()
        print(f"  saved: {OUT}")
        sys.exit(130)
