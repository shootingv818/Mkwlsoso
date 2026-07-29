// File send bridge for Eitaa Web (upload once, resend many, no re-upload).
//
// Confirmed on the live account (see capture/bridge_file.*): a file can be
// uploaded ONCE via appMessagesManager.sendFile, and then delivered to any
// number of recipients with messages.sendMedia referencing the already-
// uploaded document -- no re-upload, no "forwarded from" header, and a real
// server ACK. This is what removes the per-recipient upload load that used to
// crash the small server.
//
//   window.__MKWL_fileInit(b64, filename, mime)  -> uploads ONCE to Saved
//       Messages (tagged with an internal marker so we can locate it), stores
//       the resulting document. Returns { ok, msg_id, doc_id, code }.
//   window.__MKWL_fileSend(peerId, caption)      -> sends that document to one
//       recipient via sendMedia (auto-refreshes file_reference on expiry).
//       Returns { ok, method, msg_id, limit, code }.
(() => {
  if (window.__MKWL_fileInit) return;

  function randId() {
    try { const a = new Uint32Array(2); crypto.getRandomValues(a);
      return a[0].toString() + a[1].toString().padStart(10, "0"); }
    catch (e) { return String(Date.now()) + "007"; }
  }
  function errCode(e) {
    try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return "ERR"; }
  }
  function isLimit(s) {
    s = String(s || "").toUpperCase();
    return s.indexOf("FLOOD") >= 0 || s.indexOf("PEER_FLOOD") >= 0
      || s.indexOf("SPAM") >= 0 || s.indexOf("TOO_MANY") >= 0
      || s.indexOf("SLOWMODE") >= 0;
  }
  function extractId(res) {
    try {
      if (!res) return null;
      if (res.id != null) return res.id;
      const ups = res.updates || (res.update ? [res.update] : null);
      if (ups && ups.length) {
        for (let i = 0; i < ups.length; i++) {
          const u = ups[i];
          if (!u) continue;
          if (u.id != null) return u.id;
          if (u.message && u.message.id != null) return u.message.id;
        }
      }
    } catch (e) {}
    return null;
  }
  function getSelfId() {
    const c = [];
    try { c.push(window.appPeersManager && window.appPeersManager.peerId); } catch (e) {}
    try { c.push(window.appImManager && window.appImManager.myId); } catch (e) {}
    try { if (window.appUsersManager && window.appUsersManager.getSelf) {
      const s = window.appUsersManager.getSelf(); c.push(s && (s.id != null ? s.id : s)); } } catch (e) {}
    for (const x of c) { if (x != null && ["number", "string", "bigint"].includes(typeof x)) return x; }
    return null;
  }
  function inPeer(pid) {
    const APM = window.appPeersManager;
    const tries = [
      () => APM && APM.getInputPeerById && APM.getInputPeerById(pid),
      () => APM && APM.getInputPeerById && APM.getInputPeerById(+pid),
      () => APM && APM.getInputPeer && APM.getInputPeer(pid)
    ];
    for (let i = 0; i < tries.length; i++) { try { const p = tries[i](); if (p) return p; } catch (e) {} }
    return null;
  }
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  // Is an uploaded document still available for reuse? The state lives in the
  // page, so any reload/navigation wipes it and every following recipient would
  // otherwise fall back to a full per-recipient re-upload (~25s each, measured).
  window.__MKWL_fileReady = function () {
    const st = window.__MKWL_fileState;
    return !!(st && st.doc);
  };

  // Upload the file ONCE to Saved Messages and remember its document.
  // deadlineMs caps the "find my upload" phase by WALL CLOCK. It used to be a
  // fixed 90 iterations of (1s sleep + one getHistory round-trip); on a slow
  // host each iteration took ~6.7s, so a failing init burned 600 seconds.
  window.__MKWL_fileInit = async function (b64, filename, mime, deadlineMs) {
    const AM = window.apiManager, AMM = window.appMessagesManager;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    if (!AMM || !AMM.sendFile) return { ok: false, code: "no sendFile" };
    const sid = getSelfId();
    if (sid == null) return { ok: false, code: "no self id" };
    const selfNum = (typeof sid !== "number" && !isNaN(+sid)) ? +sid : sid;
    const peerSelf = { _: "inputPeerSelf" };

    let file;
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      file = new File([bytes], filename, { type: mime || "application/octet-stream" });
    } catch (e) { return { ok: false, code: "build_file:" + errCode(e) }; }

    // Tag the Saved-Messages copy with a unique internal marker so we can
    // locate exactly this upload (recipients never see this caption).
    const marker = "MKWLUP" + randId();
    try {
      await AMM.sendFile(selfNum, file, { caption: marker });
    } catch (e) { return { ok: false, code: "sendFile:" + errCode(e) }; }

    const budget = (typeof deadlineMs === "number" && deadlineMs > 0) ? deadlineMs : 180000;
    const until = Date.now() + budget;
    let doc = null, mid = null, tries = 0;
    while (mid == null && Date.now() < until) {
      await sleep(1000);
      tries++;
      try {
        const h = await AM.invokeApi("messages.getHistory", {
          peer: peerSelf, offset_id: 0, offset_date: 0, add_offset: 0,
          limit: 8, max_id: 0, min_id: 0, hash: 0 });
        const msgs = (h && h.messages) || [];
        for (let i = 0; i < msgs.length; i++) {
          const m = msgs[i];
          if (m && m.media && m.media.document && (m.message || "").indexOf(marker) !== -1) {
            mid = m.id; doc = m.media.document; break;
          }
        }
      } catch (e) { return { ok: false, code: "getHistory:" + errCode(e), tries: tries }; }
    }
    if (mid == null) {
      return { ok: false, tries: tries, waited_ms: budget,
               code: "locate_failed (upload not found within " +
                     Math.round(budget / 1000) + "s over " + tries + " checks)" };
    }

    window.__MKWL_fileState = { selfPeer: peerSelf, msgId: mid, doc: doc, marker: marker };
    return { ok: true, msg_id: mid, doc_id: doc && String(doc.id), tries: tries };
  };

  async function refreshDoc() {
    const AM = window.apiManager, st = window.__MKWL_fileState;
    if (!AM || !st) return false;
    try {
      const h = await AM.invokeApi("messages.getHistory", {
        peer: st.selfPeer, offset_id: 0, offset_date: 0, add_offset: 0,
        limit: 40, max_id: 0, min_id: 0, hash: 0 });
      const msgs = (h && h.messages) || [];
      for (let i = 0; i < msgs.length; i++) {
        const m = msgs[i];
        if (m && m.id === st.msgId && m.media && m.media.document) { st.doc = m.media.document; return true; }
      }
    } catch (e) {}
    return false;
  }

  // Deliver the already-uploaded document to one recipient (no re-upload).
  window.__MKWL_fileSend = async function (peerId, caption) {
    const AM = window.apiManager, st = window.__MKWL_fileState;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    if (!st || !st.doc) return { ok: false, code: "file not initialized" };
    const peer = inPeer(peerId);
    if (!peer) return { ok: false, code: "peer_unresolved" };

    const mkMedia = (d) => ({
      _: "inputMediaDocument",
      id: { _: "inputDocument", id: d.id, access_hash: d.access_hash, file_reference: d.file_reference }
    });
    const call = () => AM.invokeApi("messages.sendMedia", {
      peer: peer, media: mkMedia(st.doc), message: (caption || ""), random_id: randId() });

    try {
      const r = await call();
      return { ok: true, method: "sendMedia", msg_id: extractId(r) };
    } catch (e) {
      const code = errCode(e);
      if (isLimit(code)) return { ok: false, limit: true, code: code };
      if (String(code).toUpperCase().indexOf("FILE_REFERENCE") >= 0) {
        // The document's file_reference expired; refresh from Saved Messages
        // and retry once.
        const refreshed = await refreshDoc();
        if (refreshed) {
          try {
            const r2 = await call();
            return { ok: true, method: "sendMedia(refreshed)", msg_id: extractId(r2) };
          } catch (e2) {
            const c2 = errCode(e2);
            if (isLimit(c2)) return { ok: false, limit: true, code: c2 };
            return { ok: false, code: c2 };
          }
        }
        return { ok: false, code: "file_reference_expired_refresh_failed" };
      }
      return { ok: false, code: code };
    }
  };
})();
