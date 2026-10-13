// Where is Eitaa's API object? One answer for every bridge in this repo.
//
// Eitaa used to expose `window.apiManager` in the page's main world, and all
// seven bridges called `window.apiManager.invokeApi(...)` directly. Eitaa then
// shipped the multi-account build of Telegram Web K, which moves the managers
// into a SharedWorker (`mtproto.worker-*.js`) and leaves only a message port in
// the page. On that build `window.apiManager` is `undefined`, so every bridge
// broke at once and a bridge login reported "auth bridge unavailable".
//
// Measured on the live page (2026-10-11), after the managers were located:
//
//     rootScope.managers.apiManager.getConfig()                 ->   2 ms
//     rootScope.managers.apiManager.invokeApi('help.getConfig')  -> 128 ms
//     rootScope.managers.appStateManager.getState()              ->   1 ms
//
// The 128 ms one is the important one: that is a real round trip to Eitaa's
// server through the same channel auth.sendCode uses.
//
// Resolution order, and why:
//   1. `window.apiManager` first, so an older Eitaa build - or a cached one -
//      keeps working exactly as before and this change cannot regress it.
//   2. `rootScope.managers`, which is the object tweb's own code uses. Proven
//      above; it emits a task frame identical to Eitaa's own.
//   3. `createProxiedManagersForAccount(n)`, which builds a fresh proxied set
//      if rootScope has not been populated yet.
//
// Note on `typeof`: the managers object is a Proxy, so it answers "function"
// for ANY property name, including ones that do not exist. A `typeof` check
// against it proves nothing - only a call that returns a value does. That is
// why __MKWL_apiReady() checks for a resolvable object rather than probing for
// a specific method, and why the probes under deploy/ verify with real calls.
(() => {
  if (window.__MKWL_api) return;

  // Which account slot the proxied managers should be built for. Eitaa's own
  // frames carry accountNumber:1 for the first account.
  if (window.__MKWL_accountNumber == null) window.__MKWL_accountNumber = 1;
  window.__MKWL_apiSource = null;

  // The whole managers set (apiManager, appUsersManager, appPeersManager, ...)
  // on the new build, or null on a build that has none.
  window.__MKWL_managers = function () {
    try {
      const r = window.rootScope && window.rootScope.managers;
      if (r && r.apiManager) {
        window.__MKWL_apiSource = "rootScope.managers";
        return r;
      }
    } catch (e) {}
    try {
      const f = window.createProxiedManagersForAccount;
      if (typeof f === "function") {
        const m = f(window.__MKWL_accountNumber || 1);
        if (m && m.apiManager) {
          window.__MKWL_apiSource = "createProxiedManagersForAccount";
          return m;
        }
      }
    } catch (e) {}
    return null;
  };

  // The object that owns invokeApi. This is what every bridge needs.
  window.__MKWL_api = function () {
    try {
      if (window.apiManager && window.apiManager.invokeApi) {
        window.__MKWL_apiSource = "window.apiManager";
        return window.apiManager;
      }
    } catch (e) {}
    const m = window.__MKWL_managers();
    try { return m ? m.apiManager : null; } catch (e) { return null; }
  };

  // Any manager by name, old global preferred.
  //
  // CAUTION for callers: on the old build these globals were often synchronous
  // (`appUsersManager.getSelf()` returned a user); through the worker proxy the
  // same call returns a PROMISE. Code that uses a manager other than apiManager
  // must therefore await it, or keep its existing fallback path. Nothing in
  // this repo was switched over blindly for that reason.
  window.__MKWL_mgr = function (name) {
    try { if (window[name]) return window[name]; } catch (e) {}
    const m = window.__MKWL_managers();
    try { return m ? m[name] : null; } catch (e) { return null; }
  };

  // Readiness: can we resolve something that exposes invokeApi at all. The old
  // check polled for `window.apiManager` only, which is why login gave up after
  // 15 s on this build and reported "auth bridge unavailable".
  window.__MKWL_apiReady = function () {
    try {
      const a = window.__MKWL_api();
      return !!(a && typeof a.invokeApi === "function");
    } catch (e) { return false; }
  };

  // For diagnostics and error cards: say WHICH route was taken.
  window.__MKWL_apiInfo = function () {
    let ready = false, legacy = false, managers = false;
    try { legacy = !!(window.apiManager && window.apiManager.invokeApi); } catch (e) {}
    try { managers = !!window.__MKWL_managers(); } catch (e) {}
    try { ready = window.__MKWL_apiReady(); } catch (e) {}
    return {
      ready: ready,
      source: window.__MKWL_apiSource,
      legacy_global: legacy,
      managers_proxy: managers,
      account_number: window.__MKWL_accountNumber,
      has_dispatch_user_auth: !!(window.apiManagerProxy &&
        typeof window.apiManagerProxy.dispatchUserAuth === "function"),
      has_set_user_auth: !!(window.apiManager &&
        typeof window.apiManager.setUserAuth === "function")
    };
  };
})();
