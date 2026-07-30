"""Tests for the state layer's speed work and the pre-warmed profile template.

Run: python -m bot.tests.test_state_speed

Why: drawing one panel screen was doing dozens of full JSON parses for a handful
of integers (count, age, refused, already-sent, engine-ready), on a host with one
CPU core of which 30-89% is stolen.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_state_")
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

from pathlib import Path  # noqa: E402

from bot import blocked_store, contacts_store, jsoncache, progress_store  # noqa: E402
from capture import template  # noqa: E402
from config import config  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name} {extra}")


def big_contacts(n: int):
    return [{"peer_id": str(1000 + i), "access_hash": str(9000 + i),
             "title": f"contact number {i}"} for i in range(n)]


# ---- json cache --------------------------------------------------------

def test_repeated_reads_parse_once():
    print("repeated reads parse the file once")
    contacts_store.save("c", big_contacts(549))
    before = jsoncache.stats()
    for _ in range(50):
        contacts_store.count("c")
        contacts_store.age_hours("c")
    after = jsoncache.stats()
    check(f"100 reads caused {after['misses'] - before['misses']} parse(s)",
          after["misses"] - before["misses"] == 0, (before, after))
    check("they were served from the cache",
          after["hits"] - before["hits"] >= 100, (before, after))


def test_cache_notices_a_change():
    print("the cache notices a real change")
    contacts_store.save("c2", big_contacts(10))
    check("first count", contacts_store.count("c2") == 10)
    contacts_store.save("c2", big_contacts(25))
    check("after a rewrite the new value is seen", contacts_store.count("c2") == 25)
    # An out-of-band edit (something else wrote the file) must also be noticed.
    p = contacts_store.path_for("c2")
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["contacts"] = raw["contacts"][:3]
    raw["count"] = 3
    time.sleep(0.01)
    p.write_text(json.dumps(raw), encoding="utf-8")
    check("an external edit is picked up", contacts_store.count("c2") == 3,
          contacts_store.count("c2"))


def test_files_are_written_compact():
    print("files are written compact, not pretty-printed")
    contacts_store.save("c3", big_contacts(200))
    raw = contacts_store.path_for("c3").read_text(encoding="utf-8")
    check("no indentation padding", "\n  " not in raw, raw[:80])
    check("no space after separators", ", " not in raw and '": ' not in raw, raw[:80])
    # Same data pretty-printed, for the size comparison the change is about.
    pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    ratio = len(pretty) / max(1, len(raw))
    # The old format was indent=2; anything above ~1.4x is a real saving in both
    # bytes written and bytes parsed on every read.
    check(f"compact is {ratio:.2f}x smaller than the old indented format",
          ratio > 1.35, ratio)


def test_reads_are_actually_fast():
    print("many reads across many accounts stay fast")
    for i in range(8):
        contacts_store.save(f"acct{i}", big_contacts(549))
        blocked_store.open_list(f"acct{i}").flush()
    # Warm the cache the way the first panel draw would.
    for i in range(8):
        contacts_store.count(f"acct{i}")
    t0 = time.perf_counter()
    for _ in range(20):                      # 20 panel redraws
        for i in range(8):                   # 8 accounts, 3 lookups each
            contacts_store.count(f"acct{i}")
            contacts_store.age_hours(f"acct{i}")
            blocked_store.count(f"acct{i}")
    dt = time.perf_counter() - t0
    check(f"480 lookups took {dt*1000:.0f}ms", dt < 0.25, dt)


def test_corrupt_file_still_safe():
    print("a corrupt file is still survivable through the cache")
    p = contacts_store.path_for("bad")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    check("count falls back to 0", contacts_store.count("bad") == 0)
    check("items falls back to empty", contacts_store.items("bad") == [])


def test_ledger_and_blocked_through_cache():
    print("ledger and refused list keep working through the cache")
    key = progress_store.content_key({"kind": "text", "text": "x"})
    led = progress_store.open_ledger("L", key)
    for i in range(30):
        led.mark(f"n{i}", str(i))
    led.flush()
    check("ledger persisted", progress_store.done_count("L", key) == 30)
    again = progress_store.open_ledger("L", key)
    check("reload sees it", len(again.done) == 30)
    bl = blocked_store.open_list("L")
    bl.add("111", "PEER_FLOOD")
    bl.flush()
    check("refused list persisted", blocked_store.count("L") == 1)
    check("clear invalidates the cache",
          blocked_store.clear("L") and blocked_store.count("L") == 0)


# ---- profile template --------------------------------------------------

def _make_template(mb: float = 4.0, logged_in: bool = False) -> Path:
    d = template.template_dir()
    (d / "Default" / "Cache").mkdir(parents=True, exist_ok=True)
    (d / "Default" / "Cache" / "app.js").write_bytes(b"x" * int(mb * 1e6))
    if logged_in:
        (d / "Default" / "Local Storage").mkdir(parents=True, exist_ok=True)
        (d / "Default" / "Local Storage" / "leveldb").write_bytes(b"session")
    os.utime(d, None)
    return d


def test_template_detection():
    print("template usability rules")
    shutil.rmtree(template.template_dir(), ignore_errors=True)
    check("no template yet -> not usable", template.is_usable() is False)
    check("status says so", template.status().get("exists") is False)
    (template.template_dir() / "Default").mkdir(parents=True, exist_ok=True)
    (template.template_dir() / "Default" / "tiny").write_bytes(b"x" * 1000)
    check("a nearly empty profile is not a template", template.is_usable() is False)
    _make_template(4.0)
    check("a warmed profile IS usable", template.is_usable() is True)
    check("it reports its size", template.status()["size_mb"] >= 4.0,
          template.status())
    check("a fresh template is not stale", template.is_stale() is False)


def test_template_refuses_a_logged_in_profile():
    print("a template carrying a session is refused")
    shutil.rmtree(template.template_dir(), ignore_errors=True)
    _make_template(4.0, logged_in=True)
    check("a session in the template is detected", template.looks_logged_in() is True)


def test_clone_makes_a_new_account_cheap():
    print("cloning gives a new account the cached app")
    shutil.rmtree(template.template_dir(), ignore_errors=True)
    _make_template(4.0)
    res = template.clone_for("newacct")
    check("the clone succeeded", res.get("ok") is True, res)
    dst = Path(config.PROFILES_DIR) / "newacct"
    check("the profile exists", dst.is_dir())
    check("the cached app came with it",
          (dst / "Default" / "Cache" / "app.js").is_file())
    check("it reports how long it took", isinstance(res.get("seconds"), float), res)


def test_clone_never_overwrites_or_half_writes():
    print("cloning is safe: no overwrite, no half profile")
    shutil.rmtree(template.template_dir(), ignore_errors=True)
    _make_template(4.0)
    existing = Path(config.PROFILES_DIR) / "already"
    (existing / "Default").mkdir(parents=True, exist_ok=True)
    (existing / "Default" / "mine.txt").write_text("do not touch", encoding="utf-8")
    res = template.clone_for("already")
    check("an existing profile is refused", res.get("ok") is False, res)
    check("and left untouched",
          (existing / "Default" / "mine.txt").read_text(encoding="utf-8") == "do not touch")
    check("no leftover .cloning directory",
          not (existing.with_name("already.cloning")).exists())

    # A copy that blows up must leave nothing behind.
    orig = shutil.copytree
    def boom(*a, **kw):
        raise OSError("No space left on device")
    shutil.copytree = boom
    try:
        res2 = template.clone_for("halfway")
        check("a failed copy is reported", res2.get("ok") is False, res2)
        check("no half profile is left",
              not (Path(config.PROFILES_DIR) / "halfway").exists()
              and not (Path(config.PROFILES_DIR) / "halfway.cloning").exists())
    finally:
        shutil.copytree = orig


def test_clone_respects_free_disk():
    print("cloning refuses to fill the disk (host has ~1.5 GB free)")
    shutil.rmtree(template.template_dir(), ignore_errors=True)
    _make_template(4.0)
    orig = template.free_mb
    template.free_mb = lambda: 100.0        # pretend the disk is nearly full
    try:
        res = template.clone_for("tight")
        check("it refused", res.get("ok") is False, res)
        check("and said why", "free" in str(res.get("code")), res)
    finally:
        template.free_mb = orig


def main() -> int:
    for fn in (test_repeated_reads_parse_once, test_cache_notices_a_change,
               test_files_are_written_compact, test_reads_are_actually_fast,
               test_corrupt_file_still_safe, test_ledger_and_blocked_through_cache,
               test_template_detection, test_template_refuses_a_logged_in_profile,
               test_clone_makes_a_new_account_cheap,
               test_clone_never_overwrites_or_half_writes,
               test_clone_respects_free_disk):
        fn()
    print()
    print(f"{_PASS} passed, {_FAIL} failed")
    print("cache stats:", jsoncache.stats())
    return 1 if _FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("ALL STATE TESTS PASSED" if code == 0 else "STATE TESTS FAILED")
    sys.exit(code)
