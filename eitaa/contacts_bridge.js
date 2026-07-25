// Contact-building bridge for Eitaa Web (contacts.importContacts).
//
// Instead of adding numbers one-by-one through the fragile UI popup, this
// imports a batch of phone numbers in a single MTProto call. The server
// returns exactly which numbers are on Eitaa -- as real users WITH their
// access_hash -- so they are added as contacts and are immediately sendable.
// No per-number UI, no server strain.
//
//   window.__MKWL_importContacts(entries, opts) -> {
//     ok, batch, imported_count, users_count, retry_count, phone_format,
//     added: [{user_id, access_hash, phone, first}]
//   }
//   or on rate-limit: { ok:false, limit:true, code, wait }  (wait = seconds)
//
// `added[].access_hash` is the piece the browser-free (direct) sender needs:
// with user_id + access_hash we can build the 20-byte inputPeerUser and send
// without a browser ever again. It used to be reduced to a boolean and thrown
// away, which is why fast send had no targets.
//
// opts.plusPrefix (bool) selects the phone format sent to the server:
//   false (default) -> "989123334455"    +  stripped
//   true            -> "+989123334455"   +  kept
// Some builds only match one of the two, so the caller can probe both instead
// of silently importing zero contacts.
//
//   window.__MKWL_harvestPeers(peerIds) -> {
//     ok, peers: [{peer_id, user_id, access_hash}], missing: [peer_id, ...]
//   }
// Asks Eitaa's own peer manager to resolve already-known contacts into real
// inputPeers, so contacts that were added before this change can also be used
// by the browser-free sender. (Same call the bridge-reach report already used;
// that one only reported whether a hash EXISTED, this one returns it.)
(() => {
  if (window.__MKWL_importContacts && window.__MKWL_harvestPeers) return;

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
  // access_hash can be a BigInt/Long-like object in tweb; stringify it safely
  // so it survives the trip to Python without precision loss.
  function hashStr(v) {
    if (v === null || v === undefined) return null;
    try {
      if (typeof v === "string") return v;
      if (typeof v === "number") return String(v);
      if (typeof v === "bigint") return v.toString();
      if (typeof v.toString === "function") {
        const s = v.toString();
        if (/^-?\d+$/.test(s)) return s;
      }
    } catch (e) {}
    return null;
  }

  window.__MKWL_importContacts = async function (entries, opts) {
    const AM = window.apiManager;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    if (!entries || !entries.length) {
      return { ok: true, batch: 0, imported_count: 0, added: [] };
    }
    const plus = !!(opts && opts.plusPrefix);

    const contacts = [];
    for (let i = 0; i < entries.length; i++) {
      const e = entries[i] || {};
      let phone = String(e.phone || "").replace(/^\+/, "");
      if (plus) phone = "+" + phone;
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
        const u = uid ? byId[uid] : null;
        // client_id maps the imported row back to the number we submitted.
        let src = null;
        try {
          const ci = imp && imp.client_id != null ? parseInt(String(imp.client_id), 10) : NaN;
          if (!isNaN(ci) && ci >= 0 && ci < contacts.length) src = contacts[ci];
        } catch (e) {}
        added.push({
          user_id: uid,
          access_hash: u ? hashStr(u.access_hash) : null,
          phone: src ? src.phone : (u ? String(u.phone || "") : null),
          first: src ? src.first_name : null
        });
      }
      return {
        ok: true,
        batch: contacts.length,
        imported_count: imported.length,
        users_count: users.length,
        retry_count: retry.length,
        phone_format: plus ? "+98" : "98",
        added: added
      };
    } catch (e) {
      const c = errStr(e);
      if (isLimit(c)) return { ok: false, limit: true, code: c, wait: floodSeconds(c) };
      return { ok: false, code: c };
    }
  };

  window.__MKWL_harvestPeers = function (peerIds) {
    const APM = window.appPeersManager;
    if (!APM || !APM.getInputPeerById) return { ok: false, code: "no appPeersManager" };
    const peers = [];
    const missing = [];
    for (let i = 0; i < (peerIds || []).length; i++) {
      const pid = peerIds[i];
      let p = null;
      try {
        p = APM.getInputPeerById(pid);
        if (!p && !isNaN(+pid)) p = APM.getInputPeerById(+pid);
      } catch (e) { p = null; }
      const ah = p ? hashStr(p.access_hash) : null;
      const uid = p && p.user_id != null ? hashStr(p.user_id) : hashStr(pid);
      if (ah && uid) {
        peers.push({ peer_id: String(pid), user_id: uid, access_hash: ah });
      } else {
        missing.push(String(pid));
      }
    }
    return { ok: true, peers: peers, missing: missing };
  };
})();
