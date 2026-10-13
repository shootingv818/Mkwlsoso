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
  // Eitaa moved the API object into a worker, so `window.apiManager` is
  // undefined on the current build. api_resolver.js finds it (and still
  // prefers the old global when a build has one); the fallback below keeps this
  // file working even if the resolver was not injected.
  function API() {
    try { if (window.__MKWL_api) return window.__MKWL_api(); } catch (e) {}
    return window.apiManager || null;
  }
  if (window.__MKWL_send) return;

  window.__MKWL_send = async function (peerId, text, accessHash) {
    // accessHash is handed in by bot/transports.py from the contacts
    // list, because this Eitaa build has no window.appPeersManager for
    // the page to resolve a recipient with. See getInputPeer below.
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

    // Resolve peerId -> inputPeer.
    //
    // This used to rely on window.appPeersManager alone. The current Eitaa
    // build does not expose it, so every send returned "no bridge method
    // available" with the server never contacted: the peer came back null, the
    // invokeApi path was skipped, and appMessagesManager (also gone) could not
    // run either.
    //
    // The fix needs no new mechanism, because the project already has both
    // halves. The contacts list carries an access_hash for every contact
    // (eitaa/contacts_list.js, measured 124 of 124), and direct/peers.py
    // already builds a peer from a user_id plus that hash - the 20-byte
    // inputPeerUser, ctor 0xdde8a54c + user_id:long + access_hash:long. The
    // same thing one layer up is the plain TL object below, and the hash is
    // handed in by bot/transports.py from the very map the direct engine uses.
    //
    // Order: the hash we were given first (works on every build), then the old
    // global for an older build, then the worker-backed manager through the
    // resolver, which returns a promise and so must be awaited.
    async function getInputPeer(pid, ah) {
      if (ah) {
        // Pass the values through in the shape Eitaa's own API gave them to us
        // (contacts.getContacts returns long fields as strings), with a numeric
        // user_id as the second candidate.
        return [
          { _: "inputPeerUser", user_id: pid, access_hash: ah },
          { _: "inputPeerUser", user_id: +pid, access_hash: ah }
        ];
      }
      var APM = window.appPeersManager;
      var tries = [
        function () { return APM && APM.getInputPeerById && APM.getInputPeerById(pid); },
        function () { return APM && APM.getInputPeerById && APM.getInputPeerById(+pid); },
        function () { return APM && APM.getInputPeer && APM.getInputPeer(pid); },
        function () { return APM && APM.getInputPeer && APM.getInputPeer(+pid); }
      ];
      for (var i = 0; i < tries.length; i++) {
        try { var p = tries[i](); if (p) return [p]; } catch (e) {}
      }
      // Worker-backed manager: same method names, but asynchronous.
      try {
        var M = window.__MKWL_mgr ? window.__MKWL_mgr("appPeersManager") : null;
        if (M && M.getInputPeerById) {
          var q = await M.getInputPeerById(+pid || pid);
          if (q) return [q];
        }
      } catch (e) {}
      return [];
    }

    var pidNum = (typeof peerId === "string" && peerId !== "" && !isNaN(+peerId)) ? +peerId : peerId;

    // Preferred path: invokeApi with a resolved inputPeer -> explicit server ACK.
    var peers = await getInputPeer(peerId, accessHash);
    var AM0 = API();
    if (peers.length && AM0 && AM0.invokeApi) {
      for (var pi = 0; pi < peers.length; pi++) {
        try {
          var res = await AM0.invokeApi("messages.sendMessage", {
            peer: peers[pi], message: text, random_id: randId()
          });
          var id = extractId(res);
          if (id != null) {
            return { ok: true, method: "invokeApi", msg_id: id, detail: "server ack" };
          }
          // Resolved but no id -> uncertain; let sendText try for a definite path.
          window.__MKWL_lastErr = "invokeApi resolved without id";
          break;
        } catch (e) {
          var code = errCode(e);
          if (isLimit(code)) return { ok: false, limit: true, method: "invokeApi", code: code };
          window.__MKWL_lastErr = code;
          // A serialization complaint means this candidate had the wrong field
          // types, so the next one is worth trying; anything else is the
          // server's answer and retrying the same call cannot change it.
          if (!/serializ|type|long|invalid.*peer/i.test(code)) break;
        }
      }
    } else if (!peers.length) {
      window.__MKWL_lastErr = accessHash
        ? "could not build an inputPeer"
        : "no access_hash for this peer and no appPeersManager on this build";
    }

    // Fallback path: appMessagesManager.sendText resolves the peer itself and
    // runs the full app send pipeline (confirmed to deliver). On this build the
    // global is gone, so the resolver supplies the worker-backed manager.
    var AMM = window.appMessagesManager
      || (window.__MKWL_mgr ? window.__MKWL_mgr("appMessagesManager") : null);
    if (AMM && AMM.sendText) {
      try {
        await AMM.sendText(pidNum, text);
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
