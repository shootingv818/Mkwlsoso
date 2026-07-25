// Contact-building bridge for Eitaa Web (contacts.importContacts).
//
// Instead of adding numbers one-by-one through the fragile UI popup, this
// imports a batch of phone numbers in a single MTProto call. The server
// returns exactly which numbers are on Eitaa -- as real users WITH their
// access_hash -- so they are added as contacts and are immediately sendable
// via the send bridge. No per-number UI, no server strain.
//
//   window.__MKWL_importContacts(entries) -> {
//     ok, batch, imported_count, users_count, retry_count, added:[{user_id,has_hash}]
//   }
//   or on rate-limit: { ok:false, limit:true, code, wait }  (wait = seconds)
(() => {
  if (window.__MKWL_importContacts) return;

  function errStr(e) {
    try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return "ERR"; }
  }
  function isLimit(s) {
    s = String(s || "").toUpperCase();
    return s.indexOf("FLOOD") >= 0 || s.indexOf("TOO_MANY") >= 0 || s.indexOf("LIMIT") >= 0;
  }
  function floodSeconds(s) {
    const m = String(s || "").match(/FLOOD_WAIT_(\d+)/i);
    return m ? parseInt(m[1], 10) : null;
  }

  window.__MKWL_importContacts = async function (entries) {
    const AM = window.apiManager;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    if (!entries || !entries.length) return { ok: true, batch: 0, imported_count: 0, added: [] };

    const contacts = [];
    for (let i = 0; i < entries.length; i++) {
      const e = entries[i] || {};
      const phone = String(e.phone || "").replace(/^\+/, "");
      contacts.push({
        _: "inputPhoneContact",
        client_id: String(i),
        phone: phone,
        first_name: String(e.first || phone || ""),
        last_name: String(e.last || "")
      });
    }

    try {
      const res = await AM.invokeApi("contacts.importContacts", { contacts: contacts });
      const users = (res && res.users) || [];
      const imported = (res && res.imported) || [];
      const retry = (res && res.retry_contacts) || [];

      const byId = {};
      for (let i = 0; i < users.length; i++) {
        const u = users[i];
        if (u && u.id != null) byId[String(u.id)] = u;
      }
      const added = [];
      for (let i = 0; i < imported.length; i++) {
        const imp = imported[i];
        const uid = imp && imp.user_id != null ? String(imp.user_id) : null;
        added.push({
          user_id: uid,
          has_hash: !!(uid && byId[uid] && byId[uid].access_hash != null)
        });
      }
      return {
        ok: true,
        batch: contacts.length,
        imported_count: imported.length,
        users_count: users.length,
        retry_count: retry.length,
        added: added
      };
    } catch (e) {
      const c = errStr(e);
      if (isLimit(c)) return { ok: false, limit: true, code: c, wait: floodSeconds(c) };
      return { ok: false, code: c };
    }
  };
})();
