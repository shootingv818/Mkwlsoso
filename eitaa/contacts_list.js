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
      // u.status is on the wire already and used to be dropped here, exactly as
      // access_hash once was. It is what the broadcast send order is built from
      // (eitaa/send_order.py), so no extra API call is needed for tiering.
      //
      // The RAW constructor name is passed through and NOT interpreted here.
      // Python owns the mapping, because that is where it can be unit-tested
      // without a browser. was_online is forwarded verbatim including 0 -- 0
      // means "no time given", and deciding that in the page would hide it.
      const st = u.status;
      out.push({
        peer_id: String(u.id),
        access_hash: String(u.access_hash),
        title: titleOf(u),
        username: u.username ? String(u.username) : "",
        phone: u.phone ? String(u.phone) : "",
        status: (st && st._) ? String(st._) : "",
        was_online: (st && typeof st.was_online === "number") ? st.was_online : null,
        expires: (st && typeof st.expires === "number") ? st.expires : null
      });
    }

    return { ok: true, count: out.length, contacts: out, skipped: skipped,
             raw: users.length, server_now: Math.floor(Date.now() / 1000) };
  };
})();
