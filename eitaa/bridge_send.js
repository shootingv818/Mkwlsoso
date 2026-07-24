// Hybrid send bridge for Eitaa Web.
//
// Discovery + verification (see capture/bridge.*) proved that Eitaa Web exposes
// its own API managers on `window` and that a high-level send really delivers:
//   - apiManager.invokeApi('messages.sendMessage', ...)  -> updateShortSentMessage
//   - appMessagesManager.sendText(peerId, text)          -> delivers via the app
//
// This defines window.__MKWL_send(peerId, text): it sends through Eitaa's OWN
// engine (no search, no chat open, no typing, no clicking). It prefers the
// invokeApi path because that returns a server ACK containing the new message
// id -- a real, unfakeable proof of delivery. It falls back to sendText (which
// resolves the peer itself and runs the full app pipeline) when an input peer
// cannot be resolved. Server flood/limit errors are reported explicitly so the
// caller can pause instead of hammering.
//
// Returns: { ok, method, msg_id, limit, code, detail }
(() => {
  if (window.__MKWL_send) return;

  window.__MKWL_send = async function (peerId, text) {
    function randId() {
      try {
        var a = new Uint32Array(2);
        crypto.getRandomValues(a);
        return a[0].toString() + a[1].toString().padStart(10, "0");
      } catch (e) { return String(Date.now()) + "007"; }
    }

    // Pull a real message id out of whatever the API returns (ack proof).
    function extractId(res) {
      try {
        if (!res) return null;
        if (res.id != null) return res.id;                    // updateShortSentMessage
        var ups = res.updates || (res.update ? [res.update] : null);
        if (ups && ups.length) {
          for (var i = 0; i < ups.length; i++) {
            var u = ups[i];
            if (!u) continue;
            if (u.id != null) return u.id;                    // updateMessageID
            if (u.message && u.message.id != null) return u.message.id;
          }
        }
      } catch (e) {}
      return null;
    }

    function isLimit(s) {
      s = String(s || "").toUpperCase();
      return s.indexOf("FLOOD") >= 0 || s.indexOf("PEER_FLOOD") >= 0
        || s.indexOf("SPAM") >= 0 || s.indexOf("TOO_MANY") >= 0
        || s.indexOf("SLOWMODE") >= 0;
    }

    function errCode(e) {
      try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
      catch (x) { return "ERR"; }
    }

    // Resolve peerId -> inputPeer using Eitaa's own peer manager (uses cached
    // access_hash). Tries a few known method names/signatures.
    function getInputPeer(pid) {
      var APM = window.appPeersManager;
      var tries = [
        function () { return APM && APM.getInputPeerById && APM.getInputPeerById(pid); },
        function () { return APM && APM.getInputPeerById && APM.getInputPeerById(+pid); },
        function () { return APM && APM.getInputPeer && APM.getInputPeer(pid); },
        function () { return APM && APM.getInputPeer && APM.getInputPeer(+pid); }
      ];
      for (var i = 0; i < tries.length; i++) {
        try { var p = tries[i](); if (p) return p; } catch (e) {}
      }
      return null;
    }

    var pidNum = (typeof peerId === "string" && peerId !== "" && !isNaN(+peerId)) ? +peerId : peerId;

    // Preferred path: invokeApi with a resolved inputPeer -> explicit server ACK.
    var peer = getInputPeer(peerId);
    if (peer && window.apiManager && window.apiManager.invokeApi) {
      try {
        var res = await window.apiManager.invokeApi("messages.sendMessage", {
          peer: peer, message: text, random_id: randId()
        });
        var id = extractId(res);
        if (id != null) {
          return { ok: true, method: "invokeApi", msg_id: id, detail: "server ack" };
        }
        // Resolved but no id -> uncertain; let sendText try for a definite path.
        window.__MKWL_lastErr = "invokeApi resolved without id";
      } catch (e) {
        var code = errCode(e);
        if (isLimit(code)) return { ok: false, limit: true, method: "invokeApi", code: code };
        window.__MKWL_lastErr = code; // fall through to sendText
      }
    }

    // Fallback path: appMessagesManager.sendText resolves the peer itself and
    // runs the full app send pipeline (confirmed to deliver).
    if (window.appMessagesManager && window.appMessagesManager.sendText) {
      try {
        await window.appMessagesManager.sendText(pidNum, text);
        return {
          ok: true, method: "sendText", msg_id: null,
          detail: "sent via sendText", invoke_err: window.__MKWL_lastErr || null
        };
      } catch (e2) {
        var code2 = errCode(e2);
        if (isLimit(code2)) return { ok: false, limit: true, method: "sendText", code: code2 };
        return { ok: false, method: "sendText", code: code2, invoke_err: window.__MKWL_lastErr || null };
      }
    }

    return {
      ok: false, method: "none", code: "no bridge method available",
      invoke_err: window.__MKWL_lastErr || null
    };
  };
})();
