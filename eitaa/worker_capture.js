// Worker transport capture for Eitaa Web.
//
// The real MTProto network happens INSIDE the mtproto Web Worker, so main-thread
// fetch/XHR hooks only see media. This wraps window.Worker (as an init script,
// BEFORE any page script runs) so the worker is created from a small blob that:
//   1. runs a hook PRELUDE that patches self.fetch / XMLHttpRequest / WebSocket
//      inside the worker to record request+response BYTES (hex), and
//   2. importScripts() the ORIGINAL worker (by absolute URL, so its own relative
//      imports still resolve).
// The prelude reports each record over a BroadcastChannel('__mkwlw') so it never
// pollutes tweb's own worker message channel. The main thread collects them.
//
//   window.__MKWL_workerDump() -> [ {kind,url,reqHead,reqLen,resHead,resLen,dir,t} ]
(() => {
  if (window.__MKWL_wrapWorker) return;
  window.__MKWL_wrapWorker = true;
  window.__MKWL_workerReqs = [];

  try {
    var bc = new BroadcastChannel("__mkwlw");
    bc.onmessage = function (ev) {
      try {
        if (window.__MKWL_workerReqs.length < 500) window.__MKWL_workerReqs.push(ev.data);
      } catch (e) {}
    };
  } catch (e) {}

  window.__MKWL_workerDump = function () {
    var r = window.__MKWL_workerReqs;
    window.__MKWL_workerReqs = [];
    return r;
  };

  // This function's SOURCE is injected and executed inside each worker.
  function __mkwlw_prelude() {
    var bc;
    try { bc = new BroadcastChannel("__mkwlw"); } catch (e) { bc = null; }
    function hex(buf) {
      try {
        var b;
        if (buf instanceof ArrayBuffer) b = new Uint8Array(buf);
        else if (ArrayBuffer.isView(buf)) b = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
        else if (typeof buf === "string") return { text: buf.slice(0, 120), len: buf.length };
        else if (buf && buf.byteLength != null) b = new Uint8Array(buf);
        else return null;
        var h = "", n = Math.min(b.length, 96);
        for (var i = 0; i < n; i++) h += b[i].toString(16).padStart(2, "0");
        return { hex: h, len: b.length };
      } catch (e) { return null; }
    }
    function report(rec) { try { if (bc) bc.postMessage(rec); } catch (e) {} }

    // ---- fetch ----
    try {
      var of = self.fetch;
      if (of) self.fetch = function (input, init) {
        var url = (typeof input === "string") ? input : (input && input.url) || "";
        var body = init && init.body;
        var hq = body ? hex(body) : null;
        var rec = { kind: "fetch", url: String(url),
                    reqHead: hq ? (hq.hex || ("text:" + hq.text)) : null,
                    reqLen: hq ? hq.len : 0 };
        var p = of.apply(this, arguments);
        try {
          p.then(function (r) {
            r.clone().arrayBuffer().then(function (ab) {
              var rh = hex(ab); rec.resHead = rh ? rh.hex : null; rec.resLen = rh ? rh.len : 0;
              report(rec);
            }).catch(function () { report(rec); });
          }).catch(function () {});
        } catch (e) {}
        return p;
      };
    } catch (e) {}

    // ---- XHR ----
    try {
      var Oo = XMLHttpRequest.prototype.open, Os = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function (m, u) { this.__u = u; this.__m = m; return Oo.apply(this, arguments); };
      XMLHttpRequest.prototype.send = function (b) {
        var self2 = this;
        var rec = { kind: "xhr", url: String(this.__u || ""), method: this.__m,
                    reqHead: b ? (hex(b) || {}).hex : null, reqLen: (hex(b) || {}).len || 0 };
        this.addEventListener("load", function () {
          try { var rb = self2.response; var rh = (rb && rb.byteLength != null) ? hex(rb) : null;
                rec.resHead = rh ? rh.hex : null; rec.resLen = rh ? rh.len : 0; } catch (e) {}
          report(rec);
        });
        return Os.apply(this, arguments);
      };
    } catch (e) {}

    // ---- WebSocket ----
    try {
      var OWS = self.WebSocket;
      if (OWS) {
        self.WebSocket = function (url, protocols) {
          var ws = protocols !== undefined ? new OWS(url, protocols) : new OWS(url);
          report({ kind: "ws_open", url: String(url) });
          try {
            var os = ws.send.bind(ws);
            ws.send = function (data) {
              var h = hex(data);
              report({ kind: "ws_send", url: String(url), dir: "out",
                       reqHead: h ? h.hex : null, reqLen: h ? h.len : 0 });
              return os(data);
            };
            ws.addEventListener("message", function (ev) {
              var d = ev.data;
              if (d instanceof ArrayBuffer || (d && d.byteLength != null)) {
                var h = hex(d);
                report({ kind: "ws_recv", url: String(url), dir: "in",
                         resHead: h ? h.hex : null, resLen: h ? h.len : 0 });
              } else if (typeof d === "string") {
                report({ kind: "ws_recv", url: String(url), dir: "in", resHead: null, resText: d.slice(0, 80) });
              }
            });
          } catch (e) {}
          return ws;
        };
        try { self.WebSocket.prototype = OWS.prototype; } catch (e) {}
        try { self.WebSocket.CONNECTING = OWS.CONNECTING; self.WebSocket.OPEN = OWS.OPEN;
              self.WebSocket.CLOSING = OWS.CLOSING; self.WebSocket.CLOSED = OWS.CLOSED; } catch (e) {}
      }
    } catch (e) {}
  }

  var PRELUDE = "(" + __mkwlw_prelude.toString() + ")();";

  function wrap(OrigCtor) {
    var Wrapped = function (scriptURL, opts) {
      try {
        if (typeof scriptURL === "string" && scriptURL.indexOf("blob:") === 0) {
          return new OrigCtor(scriptURL, opts);   // already a blob; leave it
        }
        var base = (self.location && self.location.href) || location.href;
        var abs = new URL(scriptURL, base).href;
        var isModule = opts && opts.type === "module";
        var loader = isModule
          ? ("import(" + JSON.stringify(abs) + ");")
          : ("importScripts(" + JSON.stringify(abs) + ");");
        var blob = new Blob([PRELUDE + "\n" + loader], { type: "text/javascript" });
        var burl = URL.createObjectURL(blob);
        return new OrigCtor(burl, opts);
      } catch (e) {
        return new OrigCtor(scriptURL, opts);      // CSP/other -> unwrapped fallback
      }
    };
    try { Wrapped.prototype = OrigCtor.prototype; } catch (e) {}
    return Wrapped;
  }

  try { if (window.Worker) window.Worker = wrap(window.Worker); } catch (e) {}
  try { if (window.SharedWorker) window.SharedWorker = wrap(window.SharedWorker); } catch (e) {}
})();
