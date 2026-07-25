// In-page instrumentation for deep protocol capture.
//
// Injected BEFORE any Eitaa script runs. It wraps the network + crypto + worker
// + wasm entry points and buffers structured records. Python pulls them via
// window.__MKWL_dump().
//
// It captures raw base64 ONLY for small bodies (the Eitaa MTProto-like frames
// are ~120-200 bytes), so we can reconstruct the exact bytes offline without
// blowing up memory. This runs against the OWNER'S OWN account; the buffer is
// pulled to local, gitignored artifacts only.
(() => {
  if (window.__MKWL) return;
  var MAX_B64 = 16384; // only base64-capture bodies up to 16 KB
  var store = { records: [] };
  window.__MKWL = store;

  function now() { try { return performance.now(); } catch (e) { return Date.now(); } }

  function toB64(buf) {
    try {
      var bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
      if (bytes.byteLength === 0 || bytes.byteLength > MAX_B64) return null;
      var bin = "";
      for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      return btoa(bin);
    } catch (e) { return null; }
  }

  function sizeOf(x) {
    try {
      if (x == null) return null;
      if (x.byteLength != null) return x.byteLength;
      if (x.buffer && x.buffer.byteLength != null) return x.buffer.byteLength;
      if (typeof x === "string") return x.length;
    } catch (e) {}
    return null;
  }

  function push(r) {
    try {
      r.t = now();
      store.records.push(r);
      if (store.records.length > 8000) store.records.shift();
    } catch (e) {}
  }

  window.__MKWL_dump = function () {
    var r = store.records;
    store.records = [];
    return r;
  };

  // ---- fetch ----
  try {
    var origFetch = window.fetch;
    if (origFetch) {
      window.fetch = function (input, init) {
        var url = typeof input === "string" ? input : (input && input.url) || "";
        var method = (init && init.method) || (input && input.method) || "GET";
        var reqB64 = null, reqSize = null;
        try {
          var body = init && init.body;
          if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
            reqSize = sizeOf(body);
            reqB64 = toB64(body instanceof ArrayBuffer ? body : body.buffer);
          } else if (typeof body === "string") {
            reqSize = body.length;
          }
        } catch (e) {}
        push({ k: "fetch_req", url: url, method: method, reqSize: reqSize, reqB64: reqB64 });
        var p = origFetch.apply(this, arguments);
        try {
          p.then(function (resp) {
            try {
              var clone = resp.clone();
              clone.arrayBuffer().then(function (ab) {
                push({ k: "fetch_resp", url: url, status: resp.status, respSize: ab.byteLength, respB64: toB64(ab) });
              }).catch(function () {});
            } catch (e) {}
          }).catch(function () {});
        } catch (e) {}
        return p;
      };
    }
  } catch (e) {}

  // ---- XMLHttpRequest ----
  try {
    var OpenXHR = XMLHttpRequest.prototype.open;
    var SendXHR = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__mkwl = { method: method, url: url };
      return OpenXHR.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
      var info = this.__mkwl || {};
      var reqB64 = null, reqSize = sizeOf(body);
      try {
        if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
          reqB64 = toB64(body instanceof ArrayBuffer ? body : body.buffer);
        }
      } catch (e) {}
      push({ k: "xhr_req", url: info.url, method: info.method, reqSize: reqSize, reqB64: reqB64 });
      var self = this;
      this.addEventListener("load", function () {
        try {
          var rb = self.response;
          var b64 = (rb instanceof ArrayBuffer) ? toB64(rb) : null;
          push({ k: "xhr_resp", url: info.url, status: self.status, respSize: sizeOf(rb), respB64: b64 });
        } catch (e) {}
      });
      return SendXHR.apply(this, arguments);
    };
  } catch (e) {}

  // ---- Worker boundary (main-thread side) ----
  try {
    var OW = window.Worker;
    if (OW) {
      window.Worker = function (url, opts) {
        push({ k: "worker_new", url: String(url) });
        var w = new OW(url, opts);
        try {
          var origPost = w.postMessage.bind(w);
          w.postMessage = function (msg, transfer) {
            push({ k: "worker_post", url: String(url), size: sizeOf(msg), kind: typeof msg, b64: toB64(msg && (msg.buffer || msg)) });
            return origPost(msg, transfer);
          };
          w.addEventListener("message", function (ev) {
            var d = ev && ev.data;
            push({ k: "worker_msg", url: String(url), size: sizeOf(d), kind: typeof d, b64: toB64(d && (d.buffer || d)) });
          });
        } catch (e) {}
        return w;
      };
      window.Worker.prototype = OW.prototype;
    }
  } catch (e) {}

  // ---- WebAssembly ----
  try {
    var oi = WebAssembly.instantiate;
    WebAssembly.instantiate = function (bytes, imports) {
      try { push({ k: "wasm_instantiate", size: sizeOf(bytes) }); } catch (e) {}
      var p = oi.apply(this, arguments);
      try {
        p.then(function (res) {
          var inst = (res && res.instance) || res;
          var ex = [];
          try { ex = inst && inst.exports ? Object.keys(inst.exports).slice(0, 80) : []; } catch (e) {}
          push({ k: "wasm_ready", exports: ex });
        }).catch(function () {});
      } catch (e) {}
      return p;
    };
    var ois = WebAssembly.instantiateStreaming;
    if (ois) {
      WebAssembly.instantiateStreaming = function () {
        push({ k: "wasm_instantiate_streaming" });
        return ois.apply(this, arguments);
      };
    }
  } catch (e) {}

  // ---- crypto.subtle ----
  try {
    var cs = crypto && crypto.subtle;
    if (cs) {
      ["encrypt", "decrypt", "sign", "verify", "digest", "deriveBits", "deriveKey", "importKey", "generateKey"].forEach(function (fn) {
        var orig = cs[fn];
        if (!orig) return;
        cs[fn] = function (alg) {
          var algName = typeof alg === "string" ? alg : (alg && alg.name) || "unknown";
          var inSize = null;
          try {
            var args = arguments;
            var data = (fn === "digest") ? args[1] : (fn === "encrypt" || fn === "decrypt" || fn === "sign" || fn === "verify") ? args[args.length - 1] : null;
            inSize = sizeOf(data);
          } catch (e) {}
          push({ k: "subtle_" + fn, alg: algName, inSize: inSize });
          return orig.apply(cs, arguments);
        };
      });
    }
  } catch (e) {}

  // ---- WebSocket (in case some flows use it) ----
  try {
    var OWS = window.WebSocket;
    if (OWS) {
      window.WebSocket = function (url, protocols) {
        push({ k: "ws_new", url: String(url) });
        var ws = protocols ? new OWS(url, protocols) : new OWS(url);
        try {
          var op = ws.send.bind(ws);
          ws.send = function (data) {
            push({ k: "ws_send", url: String(url), size: sizeOf(data), b64: toB64(data && (data.buffer || data)) });
            return op(data);
          };
          ws.addEventListener("message", function (ev) {
            var d = ev && ev.data;
            push({ k: "ws_recv", url: String(url), size: sizeOf(d), b64: (d instanceof ArrayBuffer ? toB64(d) : null) });
          });
        } catch (e) {}
        return ws;
      };
      window.WebSocket.prototype = OWS.prototype;
    }
  } catch (e) {}
})();
