// Is this account logged in? Ask the app, not the DOM.
//
// `EitaaDriver.is_logged_in()` looked for eitaa/selectors.py SEARCH_INPUT, and
// every candidate there is an `input` element. Measured on the live page
// 2026-10-11: all six matched NOTHING, because this build renders its fields as
// `div[contenteditable]`. A fully logged-in app could therefore never be
// recognised, which is what produced "LOGIN NOT CONFIRMED" after a sign-in the
// server had accepted - the detector was wrong, not the login.
//
// Three answers, strongest first:
//
//   1. rootScope.getMyId() - a non-zero id means the app has a user. Cheap and
//      immune to any DOM change.
//   2. appStateManager.getState().authState - tweb's own state machine.
//      "authStateSignedIn" means logged in; "authStateSignIn" (no 'ed') is the
//      login form, which is exactly what the failing run reported.
//   3. the DOM, as a last resort: rendered chat rows.
//
// Returns {logged_in, how, my_id, auth_state, chat_rows} so a caller can log
// WHY it decided, instead of returning a bare boolean like the old check.
(async () => {
  const cap = (p, ms) => Promise.race([
    Promise.resolve(p),
    new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms || 6000))
  ]);
  const out = { logged_in: false, how: null, my_id: null, auth_state: null,
                chat_rows: 0 };

  // 1) the account's own id
  try {
    if (window.rootScope && typeof window.rootScope.getMyId === "function") {
      const id = await cap(window.rootScope.getMyId(), 6000);
      out.my_id = id;
      if (id && Number(id) !== 0) {
        out.logged_in = true;
        out.how = "rootScope.getMyId";
      }
    }
  } catch (e) { out.my_id = "ERR"; }

  // 2) tweb's auth state machine
  try {
    const m = window.__MKWL_managers ? window.__MKWL_managers() : null;
    if (m && m.appStateManager) {
      const st = await cap(m.appStateManager.getState(), 6000);
      const a = st && st.authState;
      out.auth_state = a ? (a._ || a) : null;
      if (String(out.auth_state) === "authStateSignedIn" && !out.logged_in) {
        out.logged_in = true;
        out.how = "authState";
      }
    }
    // a self lookup is the same question asked of a different manager
    if (!out.logged_in && m && m.appUsersManager) {
      const self = await cap(m.appUsersManager.getSelf(), 6000);
      if (self && self.id) {
        out.logged_in = true;
        out.how = "getSelf";
        out.my_id = self.id;
      }
    }
  } catch (e) {}

  // 3) the DOM, last
  try {
    out.chat_rows = document.querySelectorAll(".chatlist-chat").length;
    if (!out.logged_in && out.chat_rows > 0) {
      out.logged_in = true;
      out.how = "chat rows";
    }
  } catch (e) {}

  // An explicit negative is worth reporting: the login form being on screen is
  // proof of NOT logged in, and lets a caller stop waiting early.
  try {
    const onLogin = !!document.querySelector(".page-sign, .page-authCode");
    out.login_form_visible = onLogin;
  } catch (e) {}
  return out;
})()
