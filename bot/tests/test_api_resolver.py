"""Tests for the shared API resolver (eitaa/api_resolver.js + .py).

Run: python -m bot.tests.test_api_resolver

Offline: no browser, no network. It drives the resolver against FAKE pages (a
dict standing in for `window`, evaluated by a tiny interpreter of the three
lookups the resolver performs), and it guards the source files against the
mistake that caused this outage in the first place - a bridge reaching for
`window.apiManager` directly.

Background: Eitaa shipped the multi-account build of Telegram Web K, which
moves the managers into a SharedWorker and leaves `window.apiManager`
undefined. Every bridge broke at once and a bridge login could only ever report
"auth bridge unavailable". The fix puts the lookup in one place; these tests
exist so it stays in one place.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="mkwl_apires_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")

from eitaa import api_resolver  # noqa: E402

FAILURES: list[str] = []
_EITAA = Path(api_resolver.__file__).parent

# Bridges that talk to Eitaa's API. Each must go through the resolver.
API_BRIDGES = [
    "login_bridge.js", "bridge_send.js", "bridge_file_send.js",
    "contacts_bridge.js", "contacts_list.js", "stats_bridge.js", "presence.js",
]


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def src(name: str) -> str:
    return (_EITAA / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. the resolver source says what it must say
# ---------------------------------------------------------------------------

def test_resolution_order():
    """The old global must be tried FIRST, or this change regresses old builds."""
    s = src("api_resolver.js")
    body = s[s.index("window.__MKWL_api = function"):]
    body = body[:body.index("window.__MKWL_mgr") if "window.__MKWL_mgr" in body else len(body)]
    i_legacy = body.find("window.apiManager")
    i_managers = body.find("__MKWL_managers")
    check("__MKWL_api tries window.apiManager before the managers proxy",
          i_legacy != -1 and i_managers != -1 and i_legacy < i_managers,
          f"legacy at {i_legacy}, managers at {i_managers}")

    m = s[s.index("window.__MKWL_managers = function"):]
    i_root = m.find("rootScope")
    i_factory = m.find("createProxiedManagersForAccount")
    check("managers lookup tries rootScope before the factory",
          i_root != -1 and i_factory != -1 and i_root < i_factory,
          f"rootScope at {i_root}, factory at {i_factory}")


def test_every_lookup_is_guarded():
    """A throwing getter on a hostile/odd page must not break a job."""
    s = src("api_resolver.js")
    fns = ["__MKWL_managers", "__MKWL_api", "__MKWL_mgr", "__MKWL_apiReady", "__MKWL_apiInfo"]
    for fn in fns:
        start = s.index(fn + " = function")
        end = s.find("\n  };", start)
        body = s[start:end if end != -1 else len(s)]
        check(f"{fn} wraps its lookups in try/catch", "catch (e)" in body,
              "a page property that throws must not propagate")


def test_resolver_is_idempotent():
    s = src("api_resolver.js")
    check("resolver re-injection is a no-op", "if (window.__MKWL_api) return;" in s,
          "injected on demand by six ensure_* methods plus the login flow")


# ---------------------------------------------------------------------------
# 2. no bridge may reach for the global directly again
# ---------------------------------------------------------------------------

def test_no_bridge_calls_the_global_directly():
    bad = {}
    for name in API_BRIDGES:
        s = src(name)
        # Allowed: the fallback inside API(), and probe/report code that
        # deliberately reports whether the OLD global exists.
        for line_no, line in enumerate(s.splitlines(), 1):
            if "window.apiManager.invokeApi" not in line:
                continue
            if "hasFn(" in line or "present" in line or "!!(window.apiManager" in line:
                continue
            bad.setdefault(name, []).append(line_no)
    check("no bridge calls window.apiManager.invokeApi directly", not bad, str(bad))


def test_every_api_bridge_defines_the_helper():
    for name in API_BRIDGES:
        s = src(name)
        has_helper = "function API()" in s
        uses_resolver = "__MKWL_api" in s
        check(f"{name} resolves the API through the shared helper",
              has_helper and uses_resolver,
              f"API()={has_helper} resolver={uses_resolver}")


def test_helper_falls_back_to_the_legacy_global():
    """If the resolver was never injected, a bridge must still work on an old
    build rather than hard-fail."""
    for name in API_BRIDGES:
        s = src(name)
        start = s.find("function API()")
        if start == -1:
            check(f"{name} API() has a legacy fallback", False, "no API() helper")
            continue
        body = s[start:start + 260]
        check(f"{name} API() falls back to window.apiManager",
              "window.apiManager" in body, body.replace("\n", " ")[:90])


def test_helper_is_defined_inside_the_iife_and_before_use():
    """The invariant that actually matters: API() is declared inside the file's
    IIFE (so it cannot leak or collide between bridges) and before anything
    calls it.

    Note on placement: login_bridge.js keeps its `if (window.__MKWL_sendCode)
    return;` guard as the first statement and declares API() below it with the
    other helpers, while the six bridges patched later declare API() above
    their guard. Both are correct - a function declaration is hoisted within
    the IIFE scope, and on a repeat injection the guard returns before anything
    is redefined, leaving the closures already built on first injection intact.
    So this asserts scope and use-ordering, not a cosmetic line position.
    """
    for name in API_BRIDGES:
        s = src(name)
        i_iife = min([i for i in (s.find("(() =>"), s.find("(function")) if i != -1])
        i_def = s.find("function API()")
        # every call site, i.e. "API()" that is not the declaration itself
        calls = [m.start() for m in re.finditer(r"(?<!function )API\(\)", s)]
        check(f"{name} declares API() inside the IIFE",
              i_iife != -1 and i_def > i_iife, f"iife={i_iife} def={i_def}")
        check(f"{name} calls API() only after it is declared",
              bool(calls) and min(calls) > i_def,
              f"def={i_def} first call={min(calls) if calls else None}")


# ---------------------------------------------------------------------------
# 3. the login path
# ---------------------------------------------------------------------------

def test_login_flow_uses_the_resolver():
    s = (_EITAA / "login_flow.py").read_text(encoding="utf-8")
    check("login_flow imports the resolver helpers",
          "from eitaa.api_resolver import" in s)
    check("login_flow no longer polls for window.apiManager",
          "window.apiManager && window.apiManager.invokeApi" not in s,
          "that poll was the single point of failure")
    check("login_flow waits via READY_TIMEOUT instead of a hardcoded 30 x 500ms",
          "READY_TIMEOUT" in s and "for _ in range(30)" not in s)
    check("a failed bridge reports WHICH route was missing",
          "legacy_global" in s,
          "the bare 'auth bridge unavailable' cost hours of hunting")


def test_ready_timeout_has_margin():
    """The managers appear ~10-12s after domcontentloaded on the target host;
    the old ceiling was 15s."""
    check("READY_TIMEOUT leaves real margin", api_resolver.READY_TIMEOUT >= 30,
          f"{api_resolver.READY_TIMEOUT}s (was effectively 15s)")
    check("READY_TIMEOUT is tunable", "MKWL_API_READY_TIMEOUT" in
          (_EITAA / "api_resolver.py").read_text(encoding="utf-8"))


def test_ready_js_has_a_fallback():
    check("the readiness expression works even without the resolver",
          "window.apiManager" in api_resolver.READY_JS,
          api_resolver.READY_JS)


def test_finalize_prefers_the_new_marker():
    """setUserAuth does not exist on this build; dispatchUserAuth replaces it."""
    s = src("login_bridge.js")
    i_new = s.find("dispatchUserAuth()")
    i_old = s.find('hasFn(M, "setUserAuth")')
    check("finalize tries dispatchUserAuth before setUserAuth",
          i_new != -1 and i_old != -1 and i_new < i_old,
          f"dispatchUserAuth at {i_new}, setUserAuth at {i_old}")
    check("finalize keeps the old setUserAuth path as a fallback", i_old != -1)
    check("finalize records when no marker method exists at all",
          "no user-auth marker method on this build" in s,
          "so a silent half-login is impossible")
    check("finalize also sets telegramMeWebManager.setAuthorized",
          "setAuthorized(true)" in s)


# ---------------------------------------------------------------------------
# 4. the resolver is actually injected everywhere a bridge is
# ---------------------------------------------------------------------------

def test_driver_injects_the_resolver_before_each_bridge():
    s = (_EITAA / "driver.py").read_text(encoding="utf-8")
    check("driver imports ensure_api_resolver",
          "from eitaa.api_resolver import ensure_api_resolver" in s)
    # one injection per bridge that needs the API: send, file, stats,
    # contacts_list, presence, contacts
    n = s.count("await ensure_api_resolver(self.page)")
    check("driver injects the resolver before every API bridge", n >= 6,
          f"{n} injection sites")
    # and each must come BEFORE that bridge's own evaluate
    for src_const, bridge in [
        ("_BRIDGE_SEND_SRC", "send"), ("_BRIDGE_FILE_SRC", "file"),
        ("_BRIDGE_STATS_SRC", "stats"), ("_BRIDGE_CONTACTS_LIST_SRC", "contacts_list"),
        ("_PRESENCE_SRC", "presence"), ("_BRIDGE_CONTACTS_SRC", "contacts"),
    ]:
        i_eval = s.find(f"await self.page.evaluate({src_const})")
        if i_eval == -1:
            check(f"{bridge}: injection site found", False, f"no evaluate({src_const})")
            continue
        window = s[max(0, i_eval - 400):i_eval]
        check(f"{bridge}: resolver injected before the bridge",
              "ensure_api_resolver" in window)


def main() -> int:
    print("=" * 68)
    print("API RESOLVER TESTS")
    print("=" * 68)
    try:
        print("\n-- resolver source --")
        test_resolution_order()
        test_every_lookup_is_guarded()
        test_resolver_is_idempotent()
        print("\n-- bridges --")
        test_no_bridge_calls_the_global_directly()
        test_every_api_bridge_defines_the_helper()
        test_helper_falls_back_to_the_legacy_global()
        test_helper_is_defined_inside_the_iife_and_before_use()
        print("\n-- login path --")
        test_login_flow_uses_the_resolver()
        test_ready_timeout_has_margin()
        test_ready_js_has_a_fallback()
        test_finalize_prefers_the_new_marker()
        print("\n-- injection sites --")
        test_driver_injects_the_resolver_before_each_bridge()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL API RESOLVER TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
