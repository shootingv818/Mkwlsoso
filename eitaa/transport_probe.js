// Transport probe for Eitaa Web -> pins the real MTProto wire.
//
// Hooks fetch + XHR so that, during a controlled action (e.g. a send), we
// record every HTTP request that carries a binary MTProto payload: the URL,
// method, request-body length + first bytes (hex), and response length + first
// bytes (hex). From this we learn (a) the exact DC URL per shard and (b) the
// transport envelope (is the body raw auth_key_id|msg_key|... with no extra
// framing?). Text/JSON requests are ignored.
//
//   window.__MKWL_txDump() -> [ {method,url,reqLen,reqHead,respLen,respHead,t} ]
(() => {
  if (window.__MKWL_txDump) return;
  var store = [];
  var MAX = 400;

  function hexHead(buf, n) {
    try {
      var b;
      if (buf instanceof ArrayBuffer) b = new Uint8Array(buf);
      else if (ArrayBuffer.isView(buf)) b = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
      else if (typeof buf === "string") return { text: buf.slice(0, 64), len: buf.length };
      else return null;
      var h = "", m = Math.min(b.length, n || 64);
      for (var i = 0; i < m; i++) h += b[i].toString(16).padStart(2, "0");
      return { hex: h, len: b.length };
    } catch (e) { return null; }
  }

  function looksBinary(head) { return head && head.hex != null; }

  function push(rec) {
    if (store.length < MAX) store.push(rec);
  }

  window.__MKWL_txDump = function () { var r = store; store = []; return r; };

  // ---- fetch ----
  try {
    var of = window.fetch;
    window.fetch = function (input, init) {
      var url = (typeof input === "string") ? input : (input && input.url) || "";
      var method = (init && init.method) || (input && input.method) || "GET";
      var reqHead = init && init.body ? hexHead(init.body, 64) : null;
      var p = of.apply(this, arguments);
      if (looksBinary(reqHead) || /apiw|\.eitaa\.com|\/eitaa\//i.test(url)) {
        p.then(function (resp) {
          try {
            resp.clone().arrayBuffer().then(function (ab) {
              push({ method: method, url: String(url), reqLen: reqHead ? reqHead.len : 0,
                     reqHead: reqHead ? reqHead.hex : null, respLen: ab.byteLength,
                     respHead: (hexHead(ab, 64) || {}).hex, via: "fetch", t: Date.now() });
            }).catch(function () {});
          } catch (e) {}
        }).catch(function () {});
      }
      return p;
    };
  } catch (e) {}

  // ---- XHR ----
  try {
    var OpenXHR = XMLHttpRequest.prototype.open;
    var SendXHR = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) {
      this.__mkwl_m = m; this.__mkwl_u = u;
      return OpenXHR.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
      var self = this;
      var reqHead = body ? hexHead(body, 64) : null;
      var url = this.__mkwl_u || "";
      if (looksBinary(reqHead) || /apiw|\.eitaa\.com|\/eitaa\//i.test(url)) {
        try { self.responseType = self.responseType || ""; } catch (e) {}
        this.addEventListener("load", function () {
          try {
            var resp = self.response, rh = null;
            if (resp instanceof ArrayBuffer) rh = hexHead(resp, 64);
            else if (typeof self.responseText === "string") rh = { len: self.responseText.length };
            push({ method: self.__mkwl_m || "GET", url: String(url),
                   reqLen: reqHead ? reqHead.len : 0, reqHead: reqHead ? reqHead.hex : null,
                   respLen: rh ? rh.len : 0, respHead: rh ? (rh.hex || null) : null,
                   via: "xhr", t: Date.now() });
          } catch (e) {}
        });
      }
      return SendXHR.apply(this, arguments);
    };
  } catch (e) {}
})();
