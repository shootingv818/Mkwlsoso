// Contacts LIST bridge for Eitaa Web (instant, via the API).
//
// Replaces the old "open the Contacts view and scroll the virtualized list"
// approach, which was the single slowest thing in the whole bot: measured live
// at >10 minutes for ~1100 contacts, and capped by max_scrolls so big lists
// came back INCOMPLETE (1,190 of 6,436 in one account).
//
// One call to contacts.getContacts returns every contact with its id AND
// access_hash. Measured live: 1,094 contacts in 4 seconds, and all 1,094
// resolved to a valid inputPeer through Eitaa's own peer manager, so the fast
// send path works on them unchanged.
//
//   window.__MKWL_contactsList() -> { ok, count, contacts:[{peer_id, access_hash,
//                                     title, username, phone}], skipped, code }
(() => {
  if (window.__MKWL_contactsList) return;

  function errStr(e) {
    try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return "ERR"; }
  }

  function titleOf(u) {
    const first = (u.first_name || "").trim();
    const last = (u.last_name || "").trim();
    const name = (first + " " + last).trim();
    if (name) return name.slice(0, 80);
    if (u.username) return String(u.username).slice(0, 80);
    if (u.phone) return String(u.phone).slice(0, 80);
    return String(u.id);
  }

  window.__MKWL_contactsList = async function () {
    const AM = window.apiManager;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };

    let users = [];
    try {
      const c = await AM.invokeApi("contacts.getContacts", { hash: 0 });
      users = (c && c.users) || [];
    } catch (e) {
      return { ok: false, code: "getContacts:" + errStr(e) };
    }

    // Feed the users into Eitaa's own manager so peer resolution (and later
    // sendMedia/sendText) keeps working even on a freshly loaded page.
    try {
      const AUM = window.appUsersManager;
      if (AUM && AUM.saveApiUsers) AUM.saveApiUsers(users);
    } catch (e) { /* resolution was already proven to work without this */ }

    const out = [];
    let skipped = 0;
    for (let i = 0; i < users.length; i++) {
      const u = users[i];
      if (!u || u.id == null) { skipped++; continue; }
      // Deleted accounts and bots can never receive a broadcast.
      if (u.pFlags && (u.pFlags.deleted || u.pFlags.bot)) { skipped++; continue; }
      if (u.pFlags && u.pFlags.self) { skipped++; continue; }
      if (!u.access_hash) { skipped++; continue; }
      out.push({
        peer_id: String(u.id),
        access_hash: String(u.access_hash),
        title: titleOf(u),
        username: u.username ? String(u.username) : "",
        phone: u.phone ? String(u.phone) : ""
      });
    }

    return { ok: true, count: out.length, contacts: out, skipped: skipped,
             raw: users.length };
  };
})();
