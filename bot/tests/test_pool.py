"""Tests for the live warm-browser ceiling (capture/pool.py set_max_open).

Run: python -m bot.tests.test_pool

No real browser: playwright is stubbed (the pool module imports it at load).
Only the pure tuning logic is exercised -- set_max_open clamping and that
status() reflects it. Opening real sessions needs Chromium and is not done here.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_pool_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")


def _stub_playwright() -> None:
    try:
        import playwright.async_api  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    pkg = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    for name in ("BrowserContext", "CDPSession", "Page", "Locator", "Error"):
        setattr(api, name, type(name, (object,), {}))
    api.TimeoutError = type("TimeoutError", (Exception,), {})
    api.async_playwright = lambda: None
    pkg.async_api = api
    sys.modules.setdefault("playwright", pkg)
    sys.modules.setdefault("playwright.async_api", api)


_stub_playwright()

from capture.pool import SessionPool  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def test_set_max_open():
    print("\nset_max_open changes the ceiling live and clamps to >= 1")
    p = SessionPool(max_open=1)
    check("starts at 1", p.max_open == 1)
    check("set to 3 returns 3", p.set_max_open(3) == 3 and p.max_open == 3)
    check("0 clamps to 1", p.set_max_open(0) == 1)
    check("negative clamps to 1", p.set_max_open(-5) == 1)
    check("large value is honoured", p.set_max_open(4) == 4)


def test_status_reflects_it():
    print("\nstatus() reports the current ceiling")
    p = SessionPool(max_open=2)
    st = p.status()
    check("status max_open is 2", st["max_open"] == 2, str(st.get("max_open")))
    p.set_max_open(3)
    check("status updates after set", p.status()["max_open"] == 3)
    check("no warm sessions in a fresh pool", p.status()["warm"] == 0)


def test_store_setting_roundtrip():
    print("\nthe store setting persists and clamps")
    from bot.store import store
    check("default >= 1", store.pool_max_open >= 1)
    store.set_pool_max_open(3)
    check("set to 3", store.pool_max_open == 3)
    store.set_pool_max_open(0)
    check("0 clamps to 1", store.pool_max_open == 1)


def main() -> int:
    print("=" * 68)
    print("  BROWSER POOL TUNING TESTS")
    print("=" * 68)
    try:
        test_set_max_open()
        test_status_reflects_it()
        test_store_setting_roundtrip()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL BROWSER POOL TUNING TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
