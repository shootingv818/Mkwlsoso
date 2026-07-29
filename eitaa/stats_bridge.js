// Stats bridge for Eitaa Web (instant, via the API).
//
// Replaces the slow "scroll the whole contacts + chat list" approach with two
// direct MTProto calls through the bridge:
//   - contacts.getContacts        -> exact number of contacts
//   - messages.getDialogs (paged) -> count of private chats (peerUser dialogs)
//
//   window.__MKWL_stats() -> { ok, contacts, pvs, pages, code }
(() => {
  if (window.__MKWL_stats) return;

  function errStr(e) {
    try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return "ERR"; }
  }

  // Build an InputPeer for dialog paging from a raw peer + the response's
  // users/chats arrays (which carry the access_hash we need).
  function buildInputPeer(peer, users, chats) {
    try {
      if (!peer) return { _: "inputPeerEmpty" };
      if (peer._ === "peerUser") {
        const u = users.find(x => String(x.id) === String(peer.user_id));
        return u ? { _: "inputPeerUser", user_id: u.id, access_hash: u.access_hash || 0 }
                 : { _: "inputPeerEmpty" };
      }
      if (peer._ === "peerChat") {
        return { _: "inputPeerChat", chat_id: peer.chat_id };
      }
      if (peer._ === "peerChannel") {
        const c = chats.find(x => String(x.id) === String(peer.channel_id));
        return c ? { _: "inputPeerChannel", channel_id: c.id, access_hash: c.access_hash || 0 }
                 : { _: "inputPeerEmpty" };
      }
    } catch (e) {}
    return { _: "inputPeerEmpty" };
  }

  // withPvs=false returns as soon as the contacts number is known. The PV count
  // below pages through messages.getDialogs, which was measured live at 98
  // SECONDS against 4 seconds for the contacts call -- so callers that only
  // need the contacts number must not pay for it.
  window.__MKWL_stats = async function (withPvs) {
    const AM = window.apiManager;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };

    let contacts = -1;
    try {
      const c = await AM.invokeApi("contacts.getContacts", { hash: 0 });
      if (c && c.contacts && c.contacts.length != null) contacts = c.contacts.length;
      else if (c && c.users && c.users.length != null) contacts = c.users.length;
    } catch (e) { /* leave -1 */ }

    let pvs = 0, pages = 0;
    if (withPvs === false) return { ok: true, contacts: contacts, pvs: -1, pages: 0 };
    try {
      let offset_date = 0, offset_id = 0, offset_peer = { _: "inputPeerEmpty" };
      for (let loop = 0; loop < 80; loop++) {
        const d = await AM.invokeApi("messages.getDialogs", {
          folder_id: 0, offset_date: offset_date, offset_id: offset_id,
          offset_peer: offset_peer, limit: 100, hash: 0
        });
        const dialogs = (d && d.dialogs) || [];
        const messages = (d && d.messages) || [];
        const users = (d && d.users) || [];
        const chats = (d && d.chats) || [];
        pages++;
        if (!dialogs.length) break;
        for (let i = 0; i < dialogs.length; i++) {
          const p = dialogs[i].peer;
          if (p && p._ === "peerUser") pvs++;
        }
        if (dialogs.length < 100) break;

        const last = dialogs[dialogs.length - 1];
        const topId = last.top_message || 0;
        const lastMsg = messages.find(m => m.id === topId);
        offset_id = topId;
        offset_date = lastMsg ? lastMsg.date : offset_date;
        offset_peer = buildInputPeer(last.peer, users, chats);
        if (offset_peer._ === "inputPeerEmpty") break;  // can't page further safely
      }
    } catch (e) { /* keep whatever we counted */ }

    return { ok: true, contacts: contacts, pvs: pvs, pages: pages };
  };
})();
