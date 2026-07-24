// Login bridge for Eitaa Web (phone + code, no noVNC).
//
// Uses Eitaa Web's OWN apiManager (the same one that already sends messages)
// to drive the MTProto login: auth.sendCode -> auth.signIn. On a fresh profile
// tweb creates the unauthorized DC auth_key when the page loads, so these calls
// work before the user is logged in.
//
// SAFETY: sendCode is expensive/rate-limited by the server. This file NEVER
// loops or auto-retries; callers request a code exactly once. FLOOD errors are
// returned verbatim so the caller can stop and wait.
//
//   window.__MKWL_authReady()               -> bool (apiManager present)
//   window.__MKWL_authCreds()               -> {id, hash} | null (best-effort)
//   window.__MKWL_sendCode(phone, id, hash) -> {ok, phone_code_hash, type, code}
//   window.__MKWL_signIn(phone, hash, code) -> {ok, result, needs_password, code}
(() => {
  if (window.__MKWL_sendCode) return;

  function errStr(e) {
    try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return "ERR"; }
  }

  window.__MKWL_authReady = function () {
    return !!(window.apiManager && window.apiManager.invokeApi);
  };

  // Best-effort: read Eitaa Web's own api_id/api_hash from its in-page config,
  // so callers don't have to hardcode them.
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
      return { ok: true, result: res && res._ };
    } catch (e) {
      const c = errStr(e);
      return { ok: false, code: c, needs_password: c.toUpperCase().indexOf("SESSION_PASSWORD_NEEDED") >= 0 };
    }
  };
})();
