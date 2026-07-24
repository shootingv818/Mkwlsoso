// Login bridge for Eitaa Web (phone + code, no noVNC).
//
// Uses Eitaa Web's OWN apiManager (the same one that already sends messages)
// to drive the MTProto login: auth.sendCode -> auth.signIn. On a fresh profile
// tweb creates the unauthorized DC auth_key when the page loads, so these calls
// work before the user is logged in.
//
// auth.signIn authorizes the DC, but tweb's app state also needs to KNOW the
// logged-in user (save the user + set the user-auth marker) or a reload still
// shows the login page. __MKWL_finalizeAuth() performs that last step using
// tweb's own managers, and __MKWL_authProbe() reports what auth methods exist.
//
// SAFETY: sendCode is rate-limited by the server. This file NEVER loops or
// auto-retries; callers request a code exactly once. FLOOD errors are returned
// verbatim so the caller can stop and wait.
(() => {
  if (window.__MKWL_sendCode) return;

  function errStr(e) {
    try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return "ERR"; }
  }
  function hasFn(o, m) { try { return !!(o && typeof o[m] === "function"); } catch (e) { return false; } }

  window.__MKWL_authReady = function () {
    return !!(window.apiManager && window.apiManager.invokeApi);
  };

  window.__MKWL_authCreds = function () {
    try {
      const cands = [];
      if (window.Config && window.Config.App) cands.push(window.Config.App);
      if (window.App) cands.push(window.App);
      if (window.appConfig) cands.push(window.appConfig);
      for (let i = 0; i < cands.length; i++) {
        const c = cands[i];
        const id = c && (c.id || c.api_id || c.apiId);
        const hash = c && (c.hash || c.api_hash || c.apiHash);
        if (id && hash) return { id: id, hash: String(hash) };
      }
    } catch (e) {}
    return null;
  };

  // Report which auth-related managers/methods exist so we adjust from evidence.
  window.__MKWL_authProbe = function () {
    const out = {};
    out.apiManager = {
      present: !!window.apiManager,
      invokeApi: hasFn(window.apiManager, "invokeApi"),
      setUserAuth: hasFn(window.apiManager, "setUserAuth"),
      baseDcId: (function () { try { return window.apiManager && window.apiManager.baseDcId; } catch (e) { return null; } })()
    };
    out.apiManagerProxy = {
      present: !!window.apiManagerProxy,
      setUserAuth: hasFn(window.apiManagerProxy, "setUserAuth")
    };
    out.appUsersManager = {
      present: !!window.appUsersManager,
      saveApiUser: hasFn(window.appUsersManager, "saveApiUser"),
      saveApiUsers: hasFn(window.appUsersManager, "saveApiUsers")
    };
    out.appStateManager = { present: !!window.appStateManager };
    out.rootScope = { present: !!window.rootScope, dispatchEvent: hasFn(window.rootScope, "dispatchEvent") };
    try {
      out.managerKeys = Object.keys(window)
        .filter(k => /manager|rootscope|appstate|auth|storage|session/i.test(k))
        .slice(0, 50);
    } catch (e) { out.managerKeys = []; }
    return out;
  };

  // After auth.signIn, make tweb's app state recognize the logged-in user.
  async function finalizeAuth(authorization) {
    const steps = [];
    const user = authorization && authorization.user;
    const uid = user && user.id;

    // 1) Store the user object so tweb knows who we are.
    try {
      if (hasFn(window.appUsersManager, "saveApiUser")) {
        window.appUsersManager.saveApiUser(user); steps.push("saveApiUser");
      } else if (hasFn(window.appUsersManager, "saveApiUsers")) {
        window.appUsersManager.saveApiUsers([user]); steps.push("saveApiUsers");
      }
    } catch (e) { steps.push("saveApiUser:" + errStr(e)); }

    // 2) Set the user-auth marker (this is what a reload checks).
    let dcId = null;
    try { dcId = window.apiManager && window.apiManager.baseDcId; } catch (e) {}
    for (const M of [window.apiManager, window.apiManagerProxy]) {
      if (hasFn(M, "setUserAuth")) {
        try {
          M.setUserAuth(dcId != null ? { dcID: dcId, id: uid } : uid);
          steps.push("setUserAuth(obj)");
        } catch (e) {
          try { M.setUserAuth(uid); steps.push("setUserAuth(id)"); }
          catch (e2) { steps.push("setUserAuth:" + errStr(e2)); }
        }
        break;
      }
    }

    // 3) Nudge tweb to react live if it listens for this.
    try {
      if (hasFn(window.rootScope, "dispatchEvent")) {
        window.rootScope.dispatchEvent("user_auth", { dcID: dcId, id: uid });
        steps.push("dispatch:user_auth");
      }
    } catch (e) {}

    return steps;
  }

  window.__MKWL_sendCode = async function (phone, apiId, apiHash) {
    const AM = window.apiManager;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    try {
      const res = await AM.invokeApi("auth.sendCode", {
        phone_number: String(phone),
        api_id: apiId,
        api_hash: String(apiHash),
        settings: { _: "codeSettings" }
      });
      return {
        ok: true,
        phone_code_hash: res && res.phone_code_hash,
        type: res && res.type && res.type._,
        next_type: res && res.next_type && res.next_type._
      };
    } catch (e) { return { ok: false, code: errStr(e) }; }
  };

  window.__MKWL_signIn = async function (phone, hash, code) {
    const AM = window.apiManager;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    try {
      const res = await AM.invokeApi("auth.signIn", {
        phone_number: String(phone),
        phone_code_hash: String(hash),
        phone_code: String(code)
      });
      let finalize = [];
      try { finalize = await finalizeAuth(res); } catch (e) { finalize = ["finalize:" + errStr(e)]; }
      return { ok: true, result: res && res._, finalize: finalize, probe: window.__MKWL_authProbe() };
    } catch (e) {
      const c = errStr(e);
      return { ok: false, code: c, needs_password: c.toUpperCase().indexOf("SESSION_PASSWORD_NEEDED") >= 0 };
    }
  };
})();
