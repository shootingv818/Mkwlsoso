#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  PHOTO EXPORT PROBE — tests every method for finding & downloading ║
║  photos from Eitaa chats. READ-ONLY: sends nothing, changes nothing ║
╚══════════════════════════════════════════════════════════════════════╝

Usage (on the server):
    cd ~/Mkwlsoso
    .venv/bin/python probe_photo_export.py 989124089268

What it does (in order):
  1. Opens the account's browser session (same as any job)
  2. Injects a JS bridge into the page that calls Eitaa's API
  3. Runs 8 PROBES — each one tests a different method:
     A. messages.getDialogs      → list PV chats
     B. messages.getHistory      → list messages in one PV
     C. messages.search (photos) → search photos in one PV
     D. messages.searchGlobal    → search photos across ALL chats
     E. photo structure inspect  → what does media.photo look like?
     F. download via tweb        → ask tweb's own downloader
     G. download via DOM <img>   → read thumbnail from rendered page
     H. upload.getFile (raw API) → the MTProto standard way
  4. Prints a full diagnostic + saves JSON to /tmp/

⚠️  SAFE:
  - opens a session exactly like "Check Session" does
  - every API call is a READ (getDialogs, getHistory, search, getFile)
  - nothing is sent, deleted, or modified
  - the account's contacts/profile/session are NOT touched
  - if ANY probe fails, it logs the error and moves to the next

Delete this file after probing: it is NOT part of the bot.
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

# The mega JS bridge that runs ALL probes in one evaluate() call.
PROBE_JS = r"""
async () => {
  const AM = window.apiManager;
  const out = { ok: false, probes: {}, errors: [] };

  function err(label, e) {
    const s = String((e && (e.type || e.error_message || e.code || e.message)) || e);
    out.errors.push(label + ': ' + s);
    return s;
  }
  function hashStr(v) {
    if (v == null) return null;
    try { return String(v); } catch (e) { return null; }
  }

  if (!AM || !AM.invokeApi) {
    out.errors.push('FATAL: no window.apiManager.invokeApi');
    return out;
  }
  out.ok = true;

  // ═══════════════ PROBE A: getDialogs (find PV chats) ═══════════════
  try {
    const d = await AM.invokeApi('messages.getDialogs', {
      folder_id: 0, offset_date: 0, offset_id: 0,
      offset_peer: { _: 'inputPeerEmpty' }, limit: 30, hash: 0
    });
    const dialogs = (d && d.dialogs) || [];
    const users = (d && d.users) || [];
    const pvs = [];
    for (const dlg of dialogs) {
      if (dlg.peer && dlg.peer._ === 'peerUser') {
        const uid = dlg.peer.user_id;
        const u = users.find(x => String(x.id) === String(uid));
        pvs.push({
          user_id: String(uid),
          name: u ? ((u.first_name || '') + ' ' + (u.last_name || '')).trim() : String(uid),
          access_hash: u ? hashStr(u.access_hash) : null,
          top_message: dlg.top_message
        });
      }
    }
    out.probes.A_dialogs = { ok: true, total_dialogs: dialogs.length, pvs: pvs.slice(0, 10) };
  } catch (e) { out.probes.A_dialogs = { ok: false, error: err('A', e) }; }

  // Pick the FIRST PV that has an access_hash for the next probes.
  const targetPV = (out.probes.A_dialogs && out.probes.A_dialogs.pvs || [])
    .find(p => p.access_hash);
  if (!targetPV) {
    out.errors.push('no usable PV found for further probes');
    return out;
  }
  const peer = { _: 'inputPeerUser', user_id: +targetPV.user_id,
                 access_hash: targetPV.access_hash };
  out._target = targetPV;

  // ═══════════════ PROBE B: getHistory (recent messages in one PV) ═══════════════
  try {
    const h = await AM.invokeApi('messages.getHistory', {
      peer: peer, offset_id: 0, offset_date: 0, add_offset: 0,
      limit: 20, max_id: 0, min_id: 0, hash: 0
    });
    const msgs = (h && h.messages) || [];
    const summary = msgs.map(m => ({
      id: m.id,
      date: m.date,
      out: !!(m.pFlags && m.pFlags.out),
      has_media: !!m.media,
      media_type: m.media ? m.media._ : null,
      has_photo: !!(m.media && (m.media._ === 'messageMediaPhoto' || m.media.photo)),
    }));
    out.probes.B_history = { ok: true, count: msgs.length, messages: summary };
  } catch (e) { out.probes.B_history = { ok: false, error: err('B', e) }; }

  // ═══════════════ PROBE C: messages.search with photo filter in one PV ═══════════════
  try {
    const s = await AM.invokeApi('messages.search', {
      peer: peer,
      q: '',
      filter: { _: 'inputMessagesFilterPhotos' },
      min_date: 0, max_date: 0,
      offset_id: 0, add_offset: 0,
      limit: 20, max_id: 0, min_id: 0, hash: 0
    });
    const msgs = (s && s.messages) || [];
    const photos = msgs.map(m => ({
      id: m.id,
      date: m.date,
      out: !!(m.pFlags && m.pFlags.out),
      photo_id: m.media && m.media.photo ? hashStr(m.media.photo.id) : null,
      access_hash: m.media && m.media.photo ? hashStr(m.media.photo.access_hash) : null,
      file_reference: m.media && m.media.photo && m.media.photo.file_reference
        ? (typeof m.media.photo.file_reference === 'string'
          ? m.media.photo.file_reference.slice(0, 40)
          : '[' + m.media.photo.file_reference.length + ' bytes]')
        : null,
      sizes: m.media && m.media.photo && m.media.photo.sizes
        ? m.media.photo.sizes.map(sz => ({ _: sz._, w: sz.w, h: sz.h, type: sz.type, size: sz.size }))
        : null,
      raw_photo_keys: m.media && m.media.photo ? Object.keys(m.media.photo) : null,
    }));
    out.probes.C_search_pv = { ok: true, count: msgs.length, photos: photos.slice(0, 5) };
  } catch (e) { out.probes.C_search_pv = { ok: false, error: err('C', e) }; }

  // ═══════════════ PROBE D: messages.searchGlobal (all chats) ═══════════════
  try {
    const g = await AM.invokeApi('messages.searchGlobal', {
      q: '',
      filter: { _: 'inputMessagesFilterPhotos' },
      min_date: 0, max_date: 0,
      offset_rate: 0, offset_peer: { _: 'inputPeerEmpty' },
      offset_id: 0, limit: 10
    });
    const msgs = (g && g.messages) || [];
    out.probes.D_search_global = {
      ok: true, count: msgs.length,
      sample: msgs.slice(0, 3).map(m => ({
        id: m.id, peer: m.peer_id || m.peer, out: !!(m.pFlags && m.pFlags.out),
        has_photo: !!(m.media && m.media.photo)
      }))
    };
  } catch (e) { out.probes.D_search_global = { ok: false, error: err('D', e) }; }

  // ═══════════════ PROBE E: photo structure deep inspect ═══════════════
  // Find the first photo from probe C or B.
  let samplePhoto = null, sampleMsg = null;
  if (out.probes.C_search_pv && out.probes.C_search_pv.ok) {
    const ph = out.probes.C_search_pv.photos.find(p => p.photo_id);
    if (ph) sampleMsg = ph;
  }
  if (!sampleMsg && out.probes.B_history && out.probes.B_history.ok) {
    // Re-fetch one message that has a photo.
    const mid = (out.probes.B_history.messages || []).find(m => m.has_photo);
    if (mid) sampleMsg = mid;
  }
  if (sampleMsg) {
    // Refetch the full message to get the raw photo object.
    try {
      const h2 = await AM.invokeApi('messages.getHistory', {
        peer: peer, offset_id: sampleMsg.id + 1, add_offset: -1,
        limit: 1, max_id: 0, min_id: 0, hash: 0
      });
      const m = (h2 && h2.messages && h2.messages[0]) || null;
      if (m && m.media && m.media.photo) {
        samplePhoto = m.media.photo;
        out.probes.E_photo_structure = {
          ok: true,
          _constructor: samplePhoto._,
          keys: Object.keys(samplePhoto),
          id: hashStr(samplePhoto.id),
          access_hash: hashStr(samplePhoto.access_hash),
          file_reference_type: typeof samplePhoto.file_reference,
          file_reference_preview: samplePhoto.file_reference
            ? (typeof samplePhoto.file_reference === 'string'
              ? samplePhoto.file_reference.slice(0, 60)
              : 'Uint8Array[' + samplePhoto.file_reference.length + ']')
            : null,
          dc_id: samplePhoto.dc_id,
          sizes_count: (samplePhoto.sizes || []).length,
          sizes: (samplePhoto.sizes || []).map(s => ({
            _: s._, type: s.type, w: s.w, h: s.h, size: s.size,
            location: s.location ? Object.keys(s.location) : null,
          })),
          has_video_sizes: !!(samplePhoto.video_sizes && samplePhoto.video_sizes.length),
        };
      } else {
        out.probes.E_photo_structure = { ok: false, error: 'refetch found no photo' };
      }
    } catch (e) { out.probes.E_photo_structure = { ok: false, error: err('E', e) }; }
  } else {
    out.probes.E_photo_structure = { ok: false, error: 'no sample photo found in earlier probes' };
  }

  // ═══════════════ PROBE F: tweb's own download manager ═══════════════
  try {
    // tweb exposes appDownloadManager or similar; try multiple known entry points.
    const DL = window.appDownloadManager || window.appDocsManager || window.appPhotosManager;
    const methods = [];
    if (DL) {
      for (const k of Object.keys(DL)) {
        if (typeof DL[k] === 'function' && /download|get|save|load|photo/i.test(k))
          methods.push(k);
      }
    }
    // Also check if mtprotoworker or apiFileManager exist.
    const AFM = window.apiFileManager;
    const afmMethods = [];
    if (AFM) {
      for (const k of Object.keys(AFM)) {
        if (typeof AFM[k] === 'function') afmMethods.push(k);
      }
    }
    out.probes.F_tweb_downloader = {
      ok: !!(DL || AFM),
      appDownloadManager: !!window.appDownloadManager,
      appDocsManager: !!window.appDocsManager,
      appPhotosManager: !!window.appPhotosManager,
      apiFileManager: !!AFM,
      dl_methods: methods.slice(0, 20),
      afm_methods: afmMethods.slice(0, 20),
      // Also check rootScope for any download-related
      mtprotoworker: !!window.mtprotoWorker,
    };
  } catch (e) { out.probes.F_tweb_downloader = { ok: false, error: err('F', e) }; }

  // ═══════════════ PROBE G: DOM <img> elements → base64 ═══════════════
  try {
    // Look for any loaded photo in the page (chat bubbles).
    const imgs = document.querySelectorAll(
      '.media-photo img, .attachment-photo img, img.media-photo, ' +
      '.bubble .attachment img, img[src*="blob:"], .media-container img');
    const found = [];
    for (let i = 0; i < Math.min(imgs.length, 5); i++) {
      const img = imgs[i];
      let b64 = null;
      try {
        const c = document.createElement('canvas');
        c.width = img.naturalWidth || img.width;
        c.height = img.naturalHeight || img.height;
        if (c.width > 0 && c.height > 0) {
          c.getContext('2d').drawImage(img, 0, 0);
          b64 = c.toDataURL('image/jpeg', 0.85);
          if (b64.length < 100) b64 = null; // probably empty/tainted
        }
      } catch (e2) { b64 = 'TAINTED:' + String(e2).slice(0, 60); }
      found.push({
        src: (img.src || '').slice(0, 80),
        w: img.naturalWidth, h: img.naturalHeight,
        b64_length: b64 ? b64.length : 0,
        b64_sample: b64 ? b64.slice(0, 80) : null,
      });
    }
    out.probes.G_dom_img = {
      ok: found.length > 0,
      img_elements: imgs.length,
      samples: found,
    };
  } catch (e) { out.probes.G_dom_img = { ok: false, error: err('G', e) }; }

  // ═══════════════ PROBE H: upload.getFile (standard MTProto download) ═══════════════
  if (samplePhoto && samplePhoto.sizes && samplePhoto.sizes.length > 0) {
    try {
      // Pick the smallest size to minimize bandwidth.
      const smallest = samplePhoto.sizes[0];
      const location = {
        _: 'inputPhotoFileLocation',
        id: samplePhoto.id,
        access_hash: samplePhoto.access_hash,
        file_reference: samplePhoto.file_reference,
        thumb_size: smallest.type || 's',
      };
      const result = await AM.invokeApi('upload.getFile', {
        location: location,
        offset: 0,
        limit: 32768,  // 32KB - just enough to prove it works
      });
      out.probes.H_getFile = {
        ok: !!(result && (result.bytes || result.length)),
        type: result ? result._ : null,
        bytes_length: result && result.bytes ? result.bytes.length || result.bytes.byteLength : 0,
        has_data: !!(result && result.bytes && (result.bytes.length || result.bytes.byteLength) > 100),
        cdn_redirect: !!(result && result._ === 'upload.fileCdnRedirect'),
        keys: result ? Object.keys(result) : [],
      };
    } catch (e) {
      out.probes.H_getFile = { ok: false, error: err('H', e), code: String(e && e.type || '') };
    }
  } else {
    out.probes.H_getFile = { ok: false, error: 'no sample photo available for getFile test' };
  }

  return out;
}
"""


async def main(account: str) -> int:
    print(f"\n{'═' * 70}")
    print(f"  PHOTO EXPORT PROBE — account {account}")
    print(f"{'═' * 70}\n")

    print("  Opening browser session (same as a normal job)...")
    t0 = time.time()
    async with session_pool.lease(account, headed=config.HEADED_JOBS) as session:
        driver = EitaaDriver(session)
        await driver.open()
        t_open = time.time() - t0
        print(f"  Session ready in {t_open:.1f}s")

        if not await driver.is_logged_in():
            print("  ❌ FATAL: account is not logged in. Cannot probe.")
            return 1

        print("  ✅ Logged in. Injecting probe bridge...\n")
        print(f"  Running {8} probes (this takes 10-30 seconds)...\n")

        try:
            result = await driver.page.evaluate(PROBE_JS)
        except Exception as exc:
            print(f"  ❌ Bridge execution failed: {type(exc).__name__}: {exc}")
            return 1

    # ─── Print results ───────────────────────────────────────────────────
    if not result or not result.get("ok"):
        print(f"  ❌ Bridge reported failure: {result}")
        return 1

    target = result.get("_target")
    if target:
        print(f"  🎯 Target PV: {target.get('name')} (id={target.get('user_id')})\n")

    probes = result.get("probes", {})
    verdicts = {}

    for key in ["A_dialogs", "B_history", "C_search_pv", "D_search_global",
                "E_photo_structure", "F_tweb_downloader", "G_dom_img", "H_getFile"]:
        p = probes.get(key, {})
        ok = p.get("ok", False)
        icon = "✅" if ok else "❌"
        verdicts[key] = ok
        print(f"  {icon} Probe {key}:")
        # Pretty-print the important fields.
        for k, v in p.items():
            if k == "ok":
                continue
            if isinstance(v, list) and len(v) > 3:
                print(f"       {k}: [{len(v)} items]")
                for item in v[:3]:
                    print(f"         {json.dumps(item, ensure_ascii=False, default=str)[:120]}")
                if len(v) > 3:
                    print(f"         ... +{len(v) - 3} more")
            elif isinstance(v, dict):
                print(f"       {k}: {json.dumps(v, ensure_ascii=False, default=str)[:140]}")
            else:
                s = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else v
                print(f"       {k}: {s[:140]}")
        print()

    if result.get("errors"):
        print("  ⚠️  Errors encountered:")
        for e in result["errors"]:
            print(f"       {e}")
        print()

    # ─── Verdict ─────────────────────────────────────────────────────────
    print(f"\n{'─' * 70}")
    print("  📊 VERDICT\n")

    can_search = verdicts.get("C_search_pv") or verdicts.get("D_search_global")
    can_download = verdicts.get("H_getFile") or verdicts.get("F_tweb_downloader") or verdicts.get("G_dom_img")
    can_filter_out = verdicts.get("B_history")

    rows = [
        ("جستجوی عکس در یک PV", verdicts.get("C_search_pv")),
        ("جستجوی عکس در همهٔ چت‌ها (سریع‌ترین)", verdicts.get("D_search_global")),
        ("تشخیص ارسالی/دریافتی (pFlags.out)", can_filter_out),
        ("دانلود عکس MTProto (upload.getFile)", verdicts.get("H_getFile")),
        ("دانلود از downloader داخلی tweb", verdicts.get("F_tweb_downloader")),
        ("خواندن از DOM (<img> → canvas)", verdicts.get("G_dom_img")),
    ]
    for label, ok in rows:
        print(f"    {'🟢' if ok else '🔴'} {label}")

    print()
    if can_search and can_download:
        print("  ✅ قابلیت ساختنی است.")
        if verdicts.get("D_search_global"):
            print("     مسیر سریع (searchGlobal) کار می‌کند → ثانیه‌ای.")
        else:
            print("     searchGlobal ندارد → باید چت‌به‌چت بگردیم (دقیقه‌ای).")
        if verdicts.get("H_getFile"):
            print("     دانلود عکس از API → بهترین کیفیت، بدون محدودیت اندازه.")
        elif verdicts.get("F_tweb_downloader"):
            print("     دانلود از tweb → باید reverse شود.")
        else:
            print("     فقط DOM canvas → فقط بندانگشتی، نه کیفیت اصلی.")
    elif can_search:
        print("  ⚠️  عکس‌ها پیدا می‌شوند ولی دانلود اثبات نشد.")
        print("     نیاز به بررسی بیشتر راه دانلود.")
    else:
        print("  ❌ قابلیت در این ساختار ممکن نیست.")
        print("     جستجوی عکس کار نکرد → بررسی دستی لازم.")

    # ─── Save full JSON ──────────────────────────────────────────────────
    out_path = Path("/tmp/photo_probe_result.json")
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n  📁 Full JSON saved: {out_path}")
    print(f"{'═' * 70}\n")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <account>")
        print(f"Example: {sys.argv[0]} 989124089268")
        sys.exit(1)
    sys.exit(asyncio.run(main(sys.argv[1])))
