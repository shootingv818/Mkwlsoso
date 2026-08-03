// Photo export bridge for Eitaa Web.
//
// Python drives this in small steps so a long export can report progress and be
// stopped: the peer list and the collected photo objects stay HERE (a
// file_reference is a Uint8Array and cannot survive a round trip to Python), and
// Python only ever asks for counts, metadata and base64 slices.
//
//   window.__MKWL_px_dialogs()            -> { ok, peers }         (paged)
//   window.__MKWL_px_scan(from, count)    -> { ok, scanned, found } (a slice)
//   window.__MKWL_px_meta()               -> { ok, photos: [...] }  (labels)
//   window.__MKWL_px_fetch(from, count)   -> { ok, images: [b64...] }
//   window.__MKWL_px_reset()              -> clears state
//
// Everything measured on the live account is encoded here:
//   * getDialogs pages until an EMPTY page. Eitaa gives 25 on page one and 100
//     after that, so a short page is NOT the end.
//   * messages.search leaves `count` null and answers with the "complete" type
//     even when it fills the limit, so photo paging also walks until a SHORT
//     page rather than believing the reply.
//   * returned == 0 means the chat holds no photos; 557 of 601 answered so.
//   * upload.getFile is fastest with no dc options and tolerates concurrency 16.
//   * a size larger than one getFile call is stitched from 512 KB chunks.
(() => {
  if (window.__MKWL_px_dialogs) return;

  const S = v => { try { return String(v); } catch (e) { return ''; } };
  const errStr = e => {
    try { return S((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return 'ERR'; }
  };
  const isFlood = s => /FLOOD|TOO_MANY|LIMIT|SLOWMODE/i.test(S(s));

  const ST = {
    peers: [],        // { id, access_hash, name, top_message }
    photos: [],       // { photo, chat, date, out, id }
    seen: new Set(),  // photo ids, for dedup across chats
    scanned: 0,
  };
  window.__MKWL_px_state = ST;

  window.__MKWL_px_reset = function () {
    ST.peers = []; ST.photos = []; ST.seen = new Set(); ST.scanned = 0;
    return { ok: true };
  };

  function api() {
    const AM = window.apiManager;
    if (!AM || !AM.invokeApi) return null;
    return AM;
  }

  // ---- step 1: every private chat, paged correctly ----------------------
  window.__MKWL_px_dialogs = async function (maxPages) {
    const AM = api();
    if (!AM) return { ok: false, code: 'no invokeApi' };
    const users = new Map();
    const peers = [];
    let offset_date = 0, offset_id = 0, offset_peer = { _: 'inputPeerEmpty' };
    let pages = 0, stop = 'max pages';
    try {
      for (let i = 0; i < (maxPages || 40); i++) {
        const d = await AM.invokeApi('messages.getDialogs', {
          folder_id: 0, offset_date: offset_date, offset_id: offset_id,
          offset_peer: offset_peer, limit: 100, hash: 0 });
        const dl = (d && d.dialogs) || [], us = (d && d.users) || [],
              ch = (d && d.chats) || [], ms = (d && d.messages) || [];
        pages++;
        for (const u of us) if (u && u.id != null) users.set(S(u.id), u);

        for (const dlg of dl) {
          const p = dlg.peer || {};
          if (p._ !== 'peerUser') continue;              // PVs only
          const u = users.get(S(p.user_id));
          if (!u || u.access_hash == null) continue;
          const f = u.pFlags || {};
          if (f.self || f.bot || f.deleted) continue;    // no bots, no self
          peers.push({
            id: S(u.id), access_hash: S(u.access_hash),
            name: ((u.first_name || '') + ' ' + (u.last_name || '')).trim()
                  || (u.phone ? S(u.phone) : S(u.id)),
            top_message: dlg.top_message || 0,
          });
        }

        // An EMPTY page is the only safe end. A short page is not.
        if (dl.length === 0) { stop = 'empty page'; break; }

        const last = dl[dl.length - 1];
        const topId = last.top_message || 0;
        const lm = ms.find(m => m.id === topId);
        const prevId = offset_id;
        offset_id = topId;
        offset_date = lm ? lm.date : offset_date;

        const lp = last.peer || {};
        if (lp._ === 'peerUser') {
          const u = users.get(S(lp.user_id));
          offset_peer = (u && u.access_hash != null)
            ? { _: 'inputPeerUser', user_id: +u.id, access_hash: u.access_hash }
            : { _: 'inputPeerEmpty' };
        } else if (lp._ === 'peerChannel') {
          const c = ch.find(x => S(x.id) === S(lp.channel_id));
          offset_peer = (c && c.access_hash != null)
            ? { _: 'inputPeerChannel', channel_id: +c.id, access_hash: c.access_hash }
            : { _: 'inputPeerEmpty' };
        } else if (lp._ === 'peerChat') {
          offset_peer = { _: 'inputPeerChat', chat_id: lp.chat_id };
        } else { stop = 'unknown peer type'; break; }

        if (offset_peer._ === 'inputPeerEmpty') { stop = 'no access_hash'; break; }
        if (offset_id === prevId) { stop = 'offset stalled'; break; }
      }
    } catch (e) {
      return { ok: false, code: errStr(e), peers: peers.length };
    }
    ST.peers = peers;
    return { ok: true, peers: peers.length, pages: pages, stop_reason: stop };
  };

  // ---- step 2: scan a SLICE of chats for photos ------------------------
  // Python calls this repeatedly so it can paint progress and honour a stop.
  window.__MKWL_px_scan = async function (from, count, opts) {
    const AM = api();
    if (!AM) return { ok: false, code: 'no invokeApi' };
    const o = opts || {};
    const conc = o.conc || 8;
    const maxPerChat = o.maxPerChat || 2000;
    const slice = ST.peers.slice(from, from + count);
    if (!slice.length) return { ok: true, scanned: 0, found: 0, done: true };

    let found = 0, floods = 0, errors = 0;

    async function scanOne(p) {
      const peer = { _: 'inputPeerUser', user_id: +p.id,
                     access_hash: p.access_hash };
      let offset_id = 0, got = 0;
      for (let page = 0; page < 40; page++) {
        let r;
        try {
          r = await AM.invokeApi('messages.search', {
            peer: peer, q: '', filter: { _: 'inputMessagesFilterPhotos' },
            min_date: o.min_date || 0, max_date: o.max_date || 0,
            offset_id: offset_id, add_offset: 0,
            limit: 100, max_id: 0, min_id: 0, hash: 0 });
        } catch (e) {
          const c = errStr(e);
          if (isFlood(c)) { floods++; } else { errors++; }
          return got;
        }
        const msgs = (r && r.messages) || [];
        // The reliable "nothing here" signal.
        if (msgs.length === 0) return got;

        for (const m of msgs) {
          if (!m.media || !m.media.photo) continue;
          const pid = S(m.media.photo.id);
          if (ST.seen.has(pid)) continue;               // dedup across chats
          ST.seen.add(pid);
          ST.photos.push({
            photo: m.media.photo, chat: p.name, date: m.date || 0,
            out: !!(m.pFlags && m.pFlags.out), id: pid, msg_id: m.id,
          });
          got++; found++;
          if (got >= maxPerChat) return got;
        }
        // Do NOT trust `count` or the reply type on this build: page until a
        // SHORT page.
        if (msgs.length < 100) return got;
        const next = msgs[msgs.length - 1].id;
        if (next === offset_id) return got;             // never spin
        offset_id = next;
      }
      return got;
    }

    for (let i = 0; i < slice.length; i += conc) {
      const batch = slice.slice(i, i + conc);
      await Promise.all(batch.map(scanOne));
      ST.scanned += batch.length;
      if (floods) break;
    }
    return { ok: true, scanned: ST.scanned, found: found,
             total_photos: ST.photos.length, floods: floods, errors: errors,
             done: (from + count) >= ST.peers.length };
  };

  // ---- step 3: labels, so Python can filter and caption ---------------
  window.__MKWL_px_meta = function () {
    return { ok: true, photos: ST.photos.map((it, i) => ({
      i: i, chat: it.chat, date: it.date, out: it.out, id: it.id,
      sizes: (it.photo.sizes || []).filter(s => s.type && s.w)
        .map(s => ({ type: s.type, w: s.w, h: s.h, bytes: s.size || 0 })),
    })) };
  };

  // ---- step 4: base64 for a slice, downloaded concurrently ------------
  window.__MKWL_px_fetch = async function (indexes, opts) {
    const AM = api();
    if (!AM) return { ok: false, code: 'no invokeApi' };
    const o = opts || {};
    const target = o.targetWidth || 320;
    const conc = o.conc || 16;
    const CHUNK = 524288;                 // 512 KB per getFile call

    function pickSize(p) {
      const s = (p.sizes || []).filter(x => x.type && x.w);
      if (!s.length) return null;
      s.sort((a, b) => Math.abs(a.w - target) - Math.abs(b.w - target));
      return s[0];
    }

    async function grab(item) {
      const p = item.photo;
      const sz = pickSize(p);
      if (!sz) return { ok: false, code: 'no size' };
      const parts = [];
      let total = 0;
      const declared = sz.size || 0;
      for (let off = 0; off < 64 * CHUNK; off += CHUNK) {
        let r;
        try {
          r = await AM.invokeApi('upload.getFile', {
            location: { _: 'inputPhotoFileLocation', id: p.id,
                        access_hash: p.access_hash,
                        file_reference: p.file_reference,
                        thumb_size: sz.type },
            offset: off, limit: CHUNK });
        } catch (e) {
          const c = errStr(e);
          return { ok: false, code: c, flood: isFlood(c) };
        }
        const b = r && r.bytes;
        const u8 = b ? new Uint8Array(b) : new Uint8Array(0);
        if (u8.length) { parts.push(u8); total += u8.length; }
        if (u8.length < CHUNK) break;                 // last chunk
        if (declared && total >= declared) break;
      }
      if (!total) return { ok: false, code: 'empty' };
      let all;
      if (parts.length === 1) { all = parts[0]; }
      else {
        all = new Uint8Array(total);
        let at = 0;
        for (const part of parts) { all.set(part, at); at += part.length; }
      }
      let s = '';
      const B = 8192;
      for (let i = 0; i < all.length; i += B)
        s += String.fromCharCode.apply(null, all.subarray(i, i + B));
      return { ok: true, b64: btoa(s), bytes: total, w: sz.w, h: sz.h };
    }

    const items = indexes.map(i => ST.photos[i]).filter(Boolean);
    const out = [];
    let floods = 0, failed = 0;
    for (let i = 0; i < items.length; i += conc) {
      const batch = items.slice(i, i + conc);
      const rs = await Promise.all(batch.map(it =>
        grab(it).catch(e => ({ ok: false, code: errStr(e) }))));
      for (const r of rs) {
        if (r.ok) { out.push({ b64: r.b64, bytes: r.bytes, w: r.w, h: r.h }); }
        else {
          failed++;
          if (r.flood) floods++;
          out.push(null);
        }
      }
      if (floods) break;
    }
    return { ok: true, images: out, failed: failed, floods: floods };
  };
})();
