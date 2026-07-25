// Bridge-discovery instrumentation for Eitaa Web.
//
// Injected BEFORE any Eitaa script runs. Its ONE job is to answer a single
// question with evidence: does a high-level send (the peer + the message TEXT)
// cross a JavaScript thread boundary (Worker / SharedWorker / MessagePort /
// BroadcastChannel) in PLAINTEXT, before encryption?
//
// In modern Telegram Web K (which Eitaa is a fork of) the API managers run
// inside a worker, and the main thread posts high-level tasks to it. If that
// is true here, a controlled send whose text is a unique MARKER will make that
// marker appear, unencrypted, in a postMessage payload we can observe from the
// main thread. That would mean we can call the same channel directly instead
// of typing into the UI.
//
// This records ONLY the structural shape of payloads (key names, types, short
// string previews, byte lengths) plus a marker flag, and small buffers as
// base64 so the Python side can check whether the marker bytes are present.
// Everything is drained to the owner's own gitignored artifacts.
(() => {
  if (window.__MKWLB) return;
  var MAX_B64 = 8192; // only base64-capture buffers up to 8 KB (frames are tiny)
  var store = { records: [] };
  window.__MKWLB = store;
  window.__MKWLB_marker = "";

  function now() { try { return performance.now(); } catch (e) { return Date.now(); } }

  function push(r) {
    try {
      r.t = now();
      store.records.push(r);
      if (store.records.length > 6000) store.records.shift();
    } catch (e) {}
  }

  window.__MKWLB_dump = function () {
    var r = store.records;
    store.records = [];
    return r;
  };

  window.__MKWLB_setMarker = function (m) {
    try { window.__MKWLB_marker = String(m || ""); } catch (e) {}
  };

  function markerHit(s) {
    try {
      var m = window.__MKWLB_marker;
      return !!(m && typeof s === "string" && s.indexOf(m) !== -1);
    } catch (e) { return false; }
  }

  function toB64(buf) {
    try {
      var bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
      if (bytes.byteLength === 0 || bytes.byteLength > MAX_B64) return null;
      var bin = "";
      for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      return btoa(bin);
    } catch (e) { return null; }
  }

  // Structural, depth-limited, cycle-safe serializer. Never throws.
  function shape(v, depth, seen) {
    try {
      if (depth > 6) return { t: "..." };
      if (v === null) return { t: "null" };
      if (v === undefined) return { t: "undef" };
      var tp = typeof v;
      if (tp === "string") {
        return {
          t: "str",
          len: v.length,
          marker: markerHit(v),
          s: v.length <= 80 ? v : v.slice(0, 48)
        };
      }
      if (tp === "number" || tp === "boolean") return { t: tp, v: v };
      if (tp === "function") return { t: "fn", name: v.name || "" };
      if (tp === "bigint") return { t: "bigint", v: String(v) };
      if (v instanceof ArrayBuffer) return { t: "ArrayBuffer", bytes: v.byteLength, b64: toB64(v) };
      if (ArrayBuffer.isView(v)) {
        var cn = (v.constructor && v.constructor.name) || "TypedArray";
        return { t: cn, bytes: v.byteLength, b64: toB64(v.buffer || v) };
      }
      if (Array.isArray(v)) {
        if (seen.indexOf(v) !== -1) return { t: "cycle" };
        seen.push(v);
        var items = [];
        for (var i = 0; i < v.length && i < 16; i++) items.push(shape(v[i], depth + 1, seen));
        return { t: "arr", len: v.length, items: items };
      }
      if (tp === "object") {
        if (seen.indexOf(v) !== -1) return { t: "cycle" };
        seen.push(v);
        var ctor = "Object";
        try { ctor = (v.constructor && v.constructor.name) || "Object"; } catch (e) {}
        var out = { t: "obj", ctor: ctor, keys: {} };
        var ks = [];
        try { ks = Object.keys(v); } catch (e) { ks = []; }
        var n = 0;
        for (var j = 0; j < ks.length && n < 48; j++) {
          var k = ks[j];
          try { out.keys[k] = shape(v[k], depth + 1, seen); } catch (e) { out.keys[k] = { t: "err" }; }
          n++;
        }
        return out;
      }
      return { t: tp };
    } catch (e) { return { t: "err" }; }
  }

  function record(kind, extra, payload) {
    var r = { k: kind };
    if (extra) { for (var kk in extra) { try { r[kk] = extra[kk]; } catch (e) {} } }
    try { r.shape = shape(payload, 0, []); } catch (e) { r.shape = { t: "err" }; }
    push(r);
  }

  // ---- Worker (main-thread side) --------------------------------------
  try {
    var OW = window.Worker;
    if (OW) {
      window.Worker = function (url, opts) {
        push({ k: "worker_new", url: String(url) });
        var w = new OW(url, opts);
        try {
          var op = w.postMessage.bind(w);
          w.postMessage = function (msg, transfer) {
            record("worker_post", { url: String(url) }, msg);
            return op(msg, transfer);
          };
          w.addEventListener("message", function (ev) {
            record("worker_msg", { url: String(url) }, ev && ev.data);
          });
        } catch (e) {}
        return w;
      };
      try { window.Worker.prototype = OW.prototype; } catch (e) {}
    }
  } catch (e) {}

  // ---- SharedWorker (communicates through its .port) ------------------
  try {
    var OSW = window.SharedWorker;
    if (OSW) {
      window.SharedWorker = function (url, opts) {
        push({ k: "sharedworker_new", url: String(url) });
        var sw = new OSW(url, opts);
        try {
          var port = sw.port;
          var op = port.postMessage.bind(port);
          port.postMessage = function (msg, transfer) {
            record("swport_post", { url: String(url) }, msg);
            return op(msg, transfer);
          };
          port.addEventListener("message", function (ev) {
            record("swport_msg", { url: String(url) }, ev && ev.data);
          });
        } catch (e) {}
        return sw;
      };
      try { window.SharedWorker.prototype = OSW.prototype; } catch (e) {}
    }
  } catch (e) {}

  // ---- MessagePort (MessageChannel used to talk to a worker) ----------
  try {
    if (window.MessagePort && MessagePort.prototype) {
      var proto = MessagePort.prototype;
      var OPP = proto.postMessage;
      proto.postMessage = function (msg, transfer) {
        try { record("port_post", {}, msg); } catch (e) {}
        return OPP.apply(this, arguments);
      };
      var desc = Object.getOwnPropertyDescriptor(proto, "onmessage");
      if (desc && desc.set) {
        Object.defineProperty(proto, "onmessage", {
          configurable: true,
          enumerable: desc.enumerable,
          get: function () { return desc.get ? desc.get.call(this) : this.__mkwlb_onmsg; },
          set: function (fn) {
            this.__mkwlb_onmsg = fn;
            var wrapped = function (ev) {
              try { record("port_msg", {}, ev && ev.data); } catch (e) {}
              return fn ? fn.apply(this, arguments) : undefined;
            };
            return desc.set.call(this, wrapped);
          }
        });
      }
    }
  } catch (e) {}

  // ---- BroadcastChannel ------------------------------------------------
  try {
    var OBC = window.BroadcastChannel;
    if (OBC) {
      window.BroadcastChannel = function (name) {
        var bc = new OBC(name);
        try {
          var op = bc.postMessage.bind(bc);
          bc.postMessage = function (msg) { record("bc_post", { name: String(name) }, msg); return op(msg); };
          bc.addEventListener("message", function (ev) { record("bc_msg", { name: String(name) }, ev && ev.data); });
        } catch (e) {}
        return bc;
      };
      try { window.BroadcastChannel.prototype = OBC.prototype; } catch (e) {}
    }
  } catch (e) {}

  // ---- Probe globals for a directly-callable send method --------------
  // Best-effort: modern tweb keeps managers inside the worker (so this may
  // return nothing and the worker capture above is what matters), but some
  // builds expose managers on window.
  window.__MKWLB_probe = function () {
    var found = [];
    var RE = /(sendmessage|sendtext|invokeapi|messages_sendmessage)/i;
    function methodsOf(obj) {
      var names = [];
      try { names = names.concat(Object.keys(obj)); } catch (e) {}
      try {
        var p = Object.getPrototypeOf(obj);
        if (p) names = names.concat(Object.getOwnPropertyNames(p));
      } catch (e) {}
      var hits = [];
      for (var i = 0; i < names.length; i++) {
        if (RE.test(names[i])) hits.push(names[i]);
      }
      return hits.slice(0, 12);
    }
    try {
      var ks = Object.keys(window).slice(0, 400);
      for (var i = 0; i < ks.length; i++) {
        var k = ks[i], val;
        try { val = window[k]; } catch (e) { continue; }
        if (typeof val === "function" && RE.test(k)) {
          found.push({ path: "window." + k, kind: "function" });
        } else if (val && typeof val === "object") {
          var hits = methodsOf(val);
          if (hits.length) found.push({ path: "window." + k, methods: hits });
        }
      }
    } catch (e) {}
    return found.slice(0, 40);
  };
})();
