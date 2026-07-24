// Session exporter for Eitaa Web -> the direct client.
//
// Eitaa Web (tweb) keeps the MTProto session in IndexedDB (and a little in
// localStorage): the per-DC auth_key, server_salt, the base DC id, and the
// logged-in user id. This reads those out so a headless Python client can
// reuse the SAME authorized session with no re-login.
//
// It does NOT guess key names: it enumerates every IndexedDB database + object
// store + entry and returns their shapes, plus localStorage, so we pin the
// exact keys from a real export. Large binary values are hex-encoded.
//
//   window.__MKWL_exportSession() -> {
//     localStorage: {k: value}, indexeddb: { dbName: { stores: { store: {k: value} } } },
//     hints: { auth_keys:[...paths...], user_ids:[...], salts:[...], dcs:[...] }
//   }
(() => {
  if (window.__MKWL_exportSession) return;

  function toHexIfBinary(v) {
    try {
      if (v instanceof ArrayBuffer) v = new Uint8Array(v);
      if (ArrayBuffer.isView(v)) {
        var b = new Uint8Array(v.buffer || v);
        var h = "";
        for (var i = 0; i < b.length; i++) h += b[i].toString(16).padStart(2, "0");
        return { __hex: h, __len: b.length };
      }
    } catch (e) {}
    return v;
  }

  function shrink(v, depth) {
    // Keep values small + JSON-safe; hex-encode binary; recurse shallowly.
    if (depth > 4) return "…";
    if (v == null) return v;
    var t = typeof v;
    if (t === "string") return v.length > 4096 ? v.slice(0, 4096) + "…" : v;
    if (t === "number" || t === "boolean") return v;
    if (v instanceof ArrayBuffer || ArrayBuffer.isView(v)) return toHexIfBinary(v);
    if (Array.isArray(v)) {
      if (v.length && v.every(function (x) { return typeof x === "number" && x >= 0 && x < 256; })) {
        // looks like a byte array
        var h = ""; for (var i = 0; i < v.length; i++) h += (v[i] & 255).toString(16).padStart(2, "0");
        return { __hex: h, __len: v.length };
      }
      return v.slice(0, 20).map(function (x) { return shrink(x, depth + 1); });
    }
    if (t === "object") {
      var o = {}; var n = 0;
      for (var k in v) { if (n++ > 40) break; try { o[k] = shrink(v[k], depth + 1); } catch (e) { o[k] = "?"; } }
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
      var result = { stores: {} };
      var req;
      try { req = indexedDB.open(name); } catch (e) { return resolve(result); }
      req.onerror = function () { resolve(result); };
      req.onsuccess = function () {
        var db = req.result;
        var stores = Array.prototype.slice.call(db.objectStoreNames || []);
        if (!stores.length) { db.close(); return resolve(result); }
        var pending = stores.length;
        var tx;
        try { tx = db.transaction(stores, "readonly"); }
        catch (e) { db.close(); return resolve(result); }
        stores.forEach(function (sn) {
          var entries = {};
          result.stores[sn] = entries;
          var store = tx.objectStore(sn);
          var cur = store.openCursor();
          var count = 0;
          cur.onerror = function () { if (--pending === 0) { db.close(); resolve(result); } };
          cur.onsuccess = function (ev) {
            var c = ev.target.result;
            if (c && count < 200) {
              count++;
              try { entries[String(c.key)] = shrink(c.value, 0); } catch (e) { entries[String(c.key)] = "?"; }
              c.continue();
            } else {
              if (--pending === 0) { db.close(); resolve(result); }
            }
          };
        });
      };
    });
  }

  window.__MKWL_exportSession = async function () {
    var out = { localStorage: readLocalStorage(), indexeddb: {}, hints: {
      auth_keys: [], user_ids: [], salts: [], dcs: []
    } };

    var dbNames = [];
    try {
      if (indexedDB.databases) {
        var dbs = await indexedDB.databases();
        dbNames = dbs.map(function (d) { return d.name; }).filter(Boolean);
      }
    } catch (e) {}
    // tweb's common db names as a fallback if enumeration is unavailable.
    ["tweb", "keyvalue", "session", "tt-data", "eitaa"].forEach(function (n) {
      if (dbNames.indexOf(n) === -1) dbNames.push(n);
    });

    for (var i = 0; i < dbNames.length; i++) {
      try { out.indexeddb[dbNames[i]] = await readDb(dbNames[i]); } catch (e) {}
    }

    // Heuristic hints: point at 256-byte (512 hex) values, user ids, 8-byte salts.
    function scan(prefix, obj) {
      for (var k in obj) {
        var v = obj[k];
        var path = prefix + "/" + k;
        if (v && typeof v === "object" && v.__hex) {
          if (v.__len === 256) out.hints.auth_keys.push(path);
          if (v.__len === 8) out.hints.salts.push(path);
        } else if (typeof v === "string" && /^[0-9a-f]{512}$/i.test(v)) {
          out.hints.auth_keys.push(path);
        } else if (/user/i.test(k) && /auth|id/i.test(k)) {
          out.hints.user_ids.push(path);
        } else if (/(^|_)dc/i.test(k) || /baseDc|dcId/i.test(k)) {
          out.hints.dcs.push(path + "=" + (typeof v === "object" ? JSON.stringify(v).slice(0, 60) : v));
        } else if (v && typeof v === "object") {
          scan(path, v);
        }
      }
    }
    try { scan("localStorage", out.localStorage); } catch (e) {}
    try {
      for (var db in out.indexeddb) {
        var stores = (out.indexeddb[db] || {}).stores || {};
        for (var s in stores) scan("idb:" + db + "/" + s, stores[s]);
      }
    } catch (e) {}

    return out;
  };
})();
