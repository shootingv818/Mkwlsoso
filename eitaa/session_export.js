// Session exporter for Eitaa Web -> the direct client.
//
// Eitaa Web (tweb) keeps the MTProto session in IndexedDB (and a bit in
// localStorage): the per-DC auth_key, server_salt, the base DC id, and the
// logged-in user id. This reads those out so a headless Python client can
// reuse the SAME authorized session with no re-login.
//
// TARGETED: it SKIPS bulk stores (users/chats/messages/dialogs/…) entirely --
// those are huge and contain per-user "photo.dc_id" noise. It only captures
// auth-relevant stores/keys, so the output stays small and clean.
//
//   window.__MKWL_exportSession() -> {
//     localStorage:{...}, indexeddb:{ db:{ stores:{ store:{...} }, skipped:{store:count} } },
//     hints:{ auth_keys:[...], salts:[...], user_ids:[...], dcs:[...] }
//   }
(() => {
  if (window.__MKWL_exportSession) return;

  // Stores that are large and irrelevant to the session/auth. Skipped (counted only).
  var BULK_STORE_RE = /^(users|chats|messages|messages_[\w]*|dialogs|history|stickers?|stickerSets|docs?|gifs?|wallpapers?|reactions?|availableReactions|animatedEmoji|recentSearch|.*cache.*|updates|pts)$/i;
  // Keys worth capturing wherever they appear.
  var AUTH_KEY_RE = /(auth_?key|server_?salt|^salt$|user_?auth|^dc$|dc_?id|dc\d|baseDc|dc_?options|time_?offset|state_?id|session|xt_instance)/i;

  function toHex(v) {
    try {
      if (v instanceof ArrayBuffer) v = new Uint8Array(v);
      if (ArrayBuffer.isView(v)) {
        var b = new Uint8Array(v.buffer || v), h = "";
        for (var i = 0; i < b.length; i++) h += b[i].toString(16).padStart(2, "0");
        return { __hex: h, __len: b.length };
      }
    } catch (e) {}
    return null;
  }

  function shrink(v, depth) {
    if (depth > 3) return "…";
    if (v == null) return v;
    var t = typeof v;
    if (t === "string") return v.length > 1024 ? v.slice(0, 1024) + "…" : v;
    if (t === "number" || t === "boolean") return v;
    var hx = toHex(v);
    if (hx) return hx;
    if (Array.isArray(v)) {
      if (v.length && v.every(function (x) { return typeof x === "number" && x >= 0 && x < 256; })) {
        var h = ""; for (var i = 0; i < v.length; i++) h += (v[i] & 255).toString(16).padStart(2, "0");
        return { __hex: h, __len: v.length };
      }
      return v.slice(0, 12).map(function (x) { return shrink(x, depth + 1); });
    }
    if (t === "object") {
      var o = {}, n = 0;
      for (var k in v) { if (n++ > 30) break; try { o[k] = shrink(v[k], depth + 1); } catch (e) { o[k] = "?"; } }
      return o;
    }
    return String(v);
  }

  function readLocalStorage() {
    var out = {};
    try {
      for (var i = 0; i < localStorage.length; i++) {
        var k = localStorage.key(i);
        var raw = localStorage.getItem(k);
        try { out[k] = shrink(JSON.parse(raw), 0); } catch (e) { out[k] = shrink(raw, 0); }
      }
    } catch (e) {}
    return out;
  }

  function readDb(name) {
    return new Promise(function (resolve) {
      var result = { stores: {}, skipped: {} };
      var req;
      try { req = indexedDB.open(name); } catch (e) { return resolve(result); }
      req.onerror = function () { resolve(result); };
      req.onsuccess = function () {
        var db = req.result;
        var stores = Array.prototype.slice.call(db.objectStoreNames || []);
        if (!stores.length) { db.close(); return resolve(result); }
        var pending = stores.length, tx;
        try { tx = db.transaction(stores, "readonly"); }
        catch (e) { db.close(); return resolve(result); }
        stores.forEach(function (sn) {
          var store = tx.objectStore(sn);
          if (BULK_STORE_RE.test(sn)) {
            // Only count bulk stores; never dump their contents.
            var cReq = store.count();
            cReq.onsuccess = function () { result.skipped[sn] = cReq.result; if (--pending === 0) { db.close(); resolve(result); } };
            cReq.onerror = function () { result.skipped[sn] = -1; if (--pending === 0) { db.close(); resolve(result); } };
            return;
          }
          var entries = {}; result.stores[sn] = entries;
          var count = 0;
          var cur = store.openCursor();
          cur.onerror = function () { if (--pending === 0) { db.close(); resolve(result); } };
          cur.onsuccess = function (ev) {
            var c = ev.target.result;
            if (c && count < 120) {
              count++;
              try { entries[String(c.key)] = shrink(c.value, 0); } catch (e) { entries[String(c.key)] = "?"; }
              c.continue();
            } else { if (--pending === 0) { db.close(); resolve(result); } }
          };
        });
      };
    });
  }

  function pushCapped(arr, val) {
    if (arr.indexOf(val) === -1 && arr.length < 15) arr.push(val);
  }

  window.__MKWL_exportSession = async function () {
    var out = { localStorage: readLocalStorage(), indexeddb: {}, hints: {
      auth_keys: [], salts: [], user_ids: [], dcs: []
    } };

    var dbNames = [];
    try {
      if (indexedDB.databases) {
        var dbs = await indexedDB.databases();
        dbNames = dbs.map(function (d) { return d.name; }).filter(Boolean);
      }
    } catch (e) {}
    ["tweb", "keyvalue", "session", "tt-data", "eitaa"].forEach(function (n) {
      if (dbNames.indexOf(n) === -1) dbNames.push(n);
    });
    for (var i = 0; i < dbNames.length; i++) {
      try { out.indexeddb[dbNames[i]] = await readDb(dbNames[i]); } catch (e) {}
    }

    // Hints: only look at captured (non-bulk) data, and only auth-relevant keys.
    function scan(prefix, obj, depth) {
      if (!obj || typeof obj !== "object" || depth > 3) return;
      for (var k in obj) {
        var v = obj[k], path = prefix + "/" + k;
        if (v && typeof v === "object" && v.__hex) {
          if (v.__len === 256) pushCapped(out.hints.auth_keys, path);
          else if (v.__len === 8) pushCapped(out.hints.salts, path);
        } else if (typeof v === "string" && /^[0-9a-f]{512}$/i.test(v)) {
          pushCapped(out.hints.auth_keys, path);
        } else if (AUTH_KEY_RE.test(k)) {
          if (/user/i.test(k)) pushCapped(out.hints.user_ids, path + "=" + (typeof v === "object" ? JSON.stringify(v).slice(0, 80) : v));
          else if (/dc/i.test(k) && typeof v !== "object") pushCapped(out.hints.dcs, path + "=" + v);
          if (v && typeof v === "object") scan(path, v, depth + 1);
        }
      }
    }
    try { scan("localStorage", out.localStorage, 0); } catch (e) {}
    try {
      for (var db in out.indexeddb) {
        var stores = (out.indexeddb[db] || {}).stores || {};
        for (var s in stores) scan("idb:" + db + "/" + s, stores[s], 0);
      }
    } catch (e) {}

    return out;
  };
})();
