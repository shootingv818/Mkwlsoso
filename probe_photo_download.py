#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  PHOTO DOWNLOAD PROBE — ROUND 2                                     ║
║  Fixes the two bugs in round 1 and drills into the DOWNLOAD path.   ║
║  READ-ONLY: sends nothing, changes nothing.                         ║
╚══════════════════════════════════════════════════════════════════════╝

Round 1 proved:
  ✅ messages.search + inputMessagesFilterPhotos works (20 photos in one PV)
  ✅ pFlags.out distinguishes sent from received
  ✅ canvas is NOT tainted (blob: URLs are readable)
  ❌ searchGlobal -> INVALID_CONSTRUCTOR (not in Eitaa's layer)
  ⚠️  upload.getFile was never actually tested (my refetch logic failed)
  ⚠️  manager methods came back empty (Object.keys misses the prototype)

Round 2 answers the ONE question that decides the whole feature:
      can we get FULL-QUALITY photo bytes out of the page?

Usage:
    cd ~/Mkwlsoso
    .venv/bin/python probe_photo_download.py 989124089268
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

PROBE_JS = r"""
async () => {
  const AM = window.apiManager;
  const out = { ok: false, probes: {}, errors: [], timings: {} };

  function err(label, e) {
    const s = String((e && (e.type || e.error_message || e.code || e.message)) || e);
    out.errors.push(label + ': ' + s);
    return s;
  }
  const S = v => { try { return String(v); } catch (e) { return null; } };
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // List methods INCLUDING the prototype chain (round 1 missed these).
  function allMethods(obj) {
    const names = new Set();
    let o = obj;
    for (let depth = 0; o && depth < 4; depth++) {
      for (const k of Object.getOwnPropertyNames(o)) {
        try { if (typeof obj[k] === 'function') names.add(k); } catch (e) {}
      }
      o = Object.getPrototypeOf(o);
    }
    return [...names];
  }

  if (!AM || !AM.invokeApi) { out.errors.push('FATAL: no apiManager'); return out; }
  out.ok = true;

  // ───────── STEP 1: find a real photo and KEEP the raw object ─────────
  let photo = null, photoMsg = null, peer = null, peerName = null;
  try {
    const t0 = performance.now();
    const d = await AM.invokeApi('messages.getDialogs', {
      folder_id: 0, offset_date: 0, offset_id: 0,
      offset_peer: { _: 'inputPeerEmpty' }, limit: 40, hash: 0 });
    out.timings.getDialogs_ms = Math.round(performance.now() - t0);
    const dialogs = (d && d.dialogs) || [], users = (d && d.users) || [];

    // Walk PVs until a photo is found.
    let scanned = 0;
    for (const dlg of dialogs) {
      if (!dlg.peer || dlg.peer._ !== 'peerUser') continue;
      const u = users.find(x => S(x.id) === S(dlg.peer.user_id));
      if (!u || !u.access_hash) continue;
      const p = { _: 'inputPeerUser', user_id: +u.id, access_hash: u.access_hash };
      scanned++;
      const t1 = performance.now();
      const s = await AM.invokeApi('messages.search', {
        peer: p, q: '', filter: { _: 'inputMessagesFilterPhotos' },
        min_date: 0, max_date: 0, offset_id: 0, add_offset: 0,
        limit: 10, max_id: 0, min_id: 0, hash: 0 });
      if (!out.timings.search_one_pv_ms)
        out.timings.search_one_pv_ms = Math.round(performance.now() - t1);
      const msgs = (s && s.messages) || [];
      const withPhoto = msgs.find(m => m.media && m.media.photo);
      if (withPhoto) {
        photo = withPhoto.media.photo;
        photoMsg = withPhoto;
        peer = p;
        peerName = ((u.first_name || '') + ' ' + (u.last_name || '')).trim() || S(u.id);
        break;
      }
      if (scanned >= 8) break;   // do not scan the whole account in a probe
    }
    out.probes.step1_find = {
      ok: !!photo, pvs_scanned: scanned, peer: peerName,
      msg_id: photoMsg ? photoMsg.id : null,
      out_flag: photoMsg ? !!(photoMsg.pFlags && photoMsg.pFlags.out) : null,
    };
  } catch (e) { out.probes.step1_find = { ok: false, error: err('step1', e) }; }

  if (!photo) { out.errors.push('no photo found; cannot test download'); return out; }

  // ───────── STEP 2: the photo's real structure + available sizes ─────────
  try {
    const fr = photo.file_reference;
    out.probes.step2_structure = {
      ok: true,
      constructor: photo._,
      id: S(photo.id),
      access_hash: S(photo.access_hash),
      dc_id: photo.dc_id,
      file_reference_type: Object.prototype.toString.call(fr),
      file_reference_len: fr ? (fr.length || fr.byteLength || String(fr).length) : 0,
      keys: Object.keys(photo),
      sizes: (photo.sizes || []).map(s => ({
        _: s._, type: s.type, w: s.w, h: s.h, size: s.size,
        bytes_inline: s.bytes ? (s.bytes.length || s.bytes.byteLength) : 0,
      })),
    };
  } catch (e) { out.probes.step2_structure = { ok: false, error: err('step2', e) }; }

  // Largest size = best quality.
  const sizes = (photo.sizes || []).filter(s => s.type && s.w);
  sizes.sort((a, b) => (b.w || 0) - (a.w || 0));
  const biggest = sizes[0] || null;
  const smallest = sizes[sizes.length - 1] || null;

  // ───────── STEP 3: upload.getFile — the standard MTProto download ─────────
  // Tried with several thumb_size values because Eitaa's layer may differ.
  const getFileTries = [];
  for (const sz of [biggest, smallest].filter(Boolean)) {
    for (const variant of ['inputPhotoFileLocation', 'inputFileLocation']) {
      try {
        const loc = variant === 'inputPhotoFileLocation'
          ? { _: 'inputPhotoFileLocation', id: photo.id,
              access_hash: photo.access_hash, file_reference: photo.file_reference,
              thumb_size: sz.type }
          : { _: 'inputFileLocation', volume_id: sz.location && sz.location.volume_id,
              local_id: sz.location && sz.location.local_id,
              secret: sz.location && sz.location.secret,
              file_reference: photo.file_reference };
        const t = performance.now();
        const r = await AM.invokeApi('upload.getFile',
          { location: loc, offset: 0, limit: 65536 },
          { dcId: photo.dc_id, fileDownload: true });
        const blen = r && r.bytes ? (r.bytes.length || r.bytes.byteLength || 0) : 0;
        getFileTries.push({
          variant, thumb_size: sz.type, w: sz.w, h: sz.h,
          ok: blen > 100, result_type: r ? r._ : null,
          bytes: blen, ms: Math.round(performance.now() - t),
          keys: r ? Object.keys(r) : [],
        });
        if (blen > 100) break;
      } catch (e) {
        getFileTries.push({ variant, thumb_size: sz.type, ok: false,
                            error: err('getFile_' + variant + '_' + sz.type, e) });
      }
    }
  }
  // Also try WITHOUT the dc options object (some builds route internally).
  if (biggest) {
    try {
      const t = performance.now();
      const r = await AM.invokeApi('upload.getFile', {
        location: { _: 'inputPhotoFileLocation', id: photo.id,
                    access_hash: photo.access_hash,
                    file_reference: photo.file_reference, thumb_size: biggest.type },
        offset: 0, limit: 65536 });
      const blen = r && r.bytes ? (r.bytes.length || r.bytes.byteLength || 0) : 0;
      getFileTries.push({ variant: 'no_dc_opts', thumb_size: biggest.type,
                          ok: blen > 100, bytes: blen, result_type: r ? r._ : null,
                          ms: Math.round(performance.now() - t) });
    } catch (e) {
      getFileTries.push({ variant: 'no_dc_opts', ok: false, error: err('getFile_nodc', e) });
    }
  }
  out.probes.step3_getFile = {
    ok: getFileTries.some(t => t.ok), tries: getFileTries };

  // ───────── STEP 4: what can tweb's own managers actually do? ─────────
  try {
    const APM = window.appPhotosManager, ADM = window.appDownloadManager,
          AFM = window.apiFileManager, ADocs = window.appDocsManager;
    out.probes.step4_managers = {
      ok: true,
      appPhotosManager: APM ? allMethods(APM).filter(m =>
        /photo|download|preload|load|url|size|blob|cache/i.test(m)) : null,
      appDownloadManager: ADM ? allMethods(ADM).filter(m =>
        /download|get|blob|url|file/i.test(m)) : null,
      apiFileManager: AFM ? allMethods(AFM).filter(m =>
        /download|get|file|blob|url/i.test(m)) : null,
      appDocsManager: ADocs ? allMethods(ADocs).filter(m =>
        /download|get|url|blob/i.test(m)) : null,
    };
  } catch (e) { out.probes.step4_managers = { ok: false, error: err('step4', e) }; }

  // ───────── STEP 5: ask tweb to load the FULL photo, then read the blob ─────────
  const twebTries = [];
  const APM = window.appPhotosManager;
  if (APM && biggest) {
    // Candidate call shapes across tweb versions.
    const attempts = [
      ['preloadPhoto', () => APM.preloadPhoto(photo, biggest)],
      ['preloadPhoto_id', () => APM.preloadPhoto(photo.id, biggest)],
      ['getPhotoDownloadOptions+download', async () => {
        const opts = APM.getPhotoDownloadOptions(photo, biggest);
        return window.appDownloadManager.download(opts);
      }],
      ['downloadPhoto', () => APM.downloadPhoto(photo.id)],
      ['getPhotoURL', () => APM.getPhotoURL(photo, biggest)],
    ];
    for (const [name, fn] of attempts) {
      try {
        const t = performance.now();
        let r = fn();
        if (r && typeof r.then === 'function') r = await Promise.race([
          r, sleep(15000).then(() => 'TIMEOUT')]);
        let url = null, blobLen = 0, kind = typeof r;
        if (r === 'TIMEOUT') { twebTries.push({ name, ok: false, error: 'timeout 15s' }); continue; }
        if (r instanceof Blob) { blobLen = r.size; url = 'Blob'; }
        else if (typeof r === 'string') url = r;
        else if (r && r.url) url = r.url;
        else if (r && r.cacheContext && r.cacheContext.url) url = r.cacheContext.url;
        // If we got a URL, fetch it and measure the real byte count.
        if (url && url !== 'Blob' && /^(blob:|https?:|data:)/.test(url)) {
          const resp = await fetch(url);
          const buf = await resp.arrayBuffer();
          blobLen = buf.byteLength;
        }
        twebTries.push({ name, ok: blobLen > 1000, kind,
                         url: url ? String(url).slice(0, 60) : null,
                         bytes: blobLen, ms: Math.round(performance.now() - t) });
        if (blobLen > 1000) break;
      } catch (e) {
        twebTries.push({ name, ok: false, error: err('tweb_' + name, e) });
      }
    }
  }
  out.probes.step5_tweb_download = {
    ok: twebTries.some(t => t.ok), tries: twebTries };

  // ───────── STEP 6: biggest image currently in the DOM (quality check) ─────────
  try {
    const imgs = [...document.querySelectorAll('img')]
      .filter(i => i.naturalWidth > 200)
      .sort((a, b) => b.naturalWidth - a.naturalWidth);
    const best = imgs[0] || null;
    let b64len = 0, tainted = false;
    if (best) {
      try {
        const c = document.createElement('canvas');
        c.width = best.naturalWidth; c.height = best.naturalHeight;
        c.getContext('2d').drawImage(best, 0, 0);
        b64len = c.toDataURL('image/jpeg', 0.9).length;
      } catch (e) { tainted = true; }
    }
    out.probes.step6_dom = {
      ok: !!best && !tainted,
      imgs_over_200px: imgs.length,
      biggest: best ? { w: best.naturalWidth, h: best.naturalHeight,
                        src: (best.src || '').slice(0, 60) } : null,
      canvas_b64_len: b64len, tainted: tainted,
    };
  } catch (e) { out.probes.step6_dom = { ok: false, error: err('step6', e) }; }

  return out;
}
"""


async def main(account: str) -> int:
    print(f"\n{'=' * 70}")
    print(f"  PHOTO DOWNLOAD PROBE (round 2) — {account}")
    print(f"{'=' * 70}\n")

    t0 = time.time()
    async with session_pool.lease(account, headed=config.HEADED_JOBS) as session:
        driver = EitaaDriver(session)
        await driver.open()
        print(f"  session ready in {time.time() - t0:.1f}s")
        if not await driver.is_logged_in():
            print("  FATAL: not logged in")
            return 1
        print("  logged in; running probes (up to ~60s)...\n")
        try:
            res = await driver.page.evaluate(PROBE_JS)
        except Exception as exc:
            print(f"  bridge failed: {type(exc).__name__}: {exc}")
            return 1

    if not res or not res.get("ok"):
        print(f"  bridge reported failure: {res}")
        return 1

    for name, p in (res.get("probes") or {}).items():
        icon = "OK  " if p.get("ok") else "FAIL"
        print(f"  [{icon}] {name}")
        for k, v in p.items():
            if k == "ok":
                continue
            if isinstance(v, list):
                print(f"        {k}:")
                for item in v[:8]:
                    print(f"          {json.dumps(item, ensure_ascii=False, default=str)[:150]}")
            else:
                print(f"        {k}: {json.dumps(v, ensure_ascii=False, default=str)[:150]}")
        print()

    if res.get("timings"):
        print(f"  timings: {res['timings']}\n")
    if res.get("errors"):
        print("  errors:")
        for e in res["errors"]:
            print(f"     {e}")
        print()

    pr = res.get("probes") or {}
    getfile_ok = (pr.get("step3_getFile") or {}).get("ok")
    tweb_ok = (pr.get("step5_tweb_download") or {}).get("ok")
    dom_ok = (pr.get("step6_dom") or {}).get("ok")
    sizes = ((pr.get("step2_structure") or {}).get("sizes") or [])
    max_w = max([s.get("w") or 0 for s in sizes], default=0)

    print(f"{'-' * 70}")
    print("  VERDICT\n")
    print(f"    {'OK ' if getfile_ok else 'NO '} upload.getFile (raw MTProto, best)")
    print(f"    {'OK ' if tweb_ok else 'NO '} tweb's own downloader")
    print(f"    {'OK ' if dom_ok else 'NO '} DOM canvas read")
    print(f"        largest size Eitaa offers for this photo: {max_w}px wide")
    print()
    if getfile_ok:
        print("  BEST PATH: upload.getFile -> full quality, no UI, parallelisable.")
    elif tweb_ok:
        print("  BEST PATH: tweb's downloader -> full quality via the app's own cache.")
    elif dom_ok:
        print("  ONLY PATH: DOM canvas -> needs the chat open + scrolled; quality is")
        print("  whatever the page rendered.")
    else:
        print("  NO download path proven. The feature cannot be built as asked.")

    out_path = Path("/tmp/photo_probe2_result.json")
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n  full JSON: {out_path}")
    print(f"{'=' * 70}\n")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <account>")
        sys.exit(1)
    try:
        sys.exit(asyncio.run(main(sys.argv[1])))
    except KeyboardInterrupt:
        sys.exit(130)
