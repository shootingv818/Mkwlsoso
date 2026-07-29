"""Offline tests for the panel/send logic (no browser, no network).

Run: python -m bot.tests.test_bot_logic

Every case here maps to a real defect that was measured on the live bot, so a
regression is caught before it reaches a campaign again.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mkwl_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")

def _stub_playwright() -> None:
    """Let these tests run without Playwright installed.

    bot.runner imports capture.browser, which imports playwright at module
    level. Nothing here launches a browser, so a stub keeps the suite runnable
    on any machine (CI, a laptop, a server before `playwright install`).
    """
    try:
        import playwright.async_api  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    import types
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

from config import config  # noqa: E402
from bot import cards, contacts_store, progress_store  # noqa: E402
from bot.runner import _flood_wait, _is_limit, _FAILURE_BRAKE, _MAX_FILE_REINITS  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name} {extra}")


# ---- contacts cache ----------------------------------------------------

def test_contacts_store() -> None:
    print("contacts_store")
    rec = contacts_store.save("acc", [
        {"peer_id": "111", "access_hash": "999", "title": "Ali"},
        {"peer_id": "111", "access_hash": "999", "title": "Ali again"},   # dupe
        {"peer_id": "222", "title": "NoHash"},
        {"title": "", "peer_id": ""},                                     # blank
    ])
    check("dedupes by peer_id", rec["count"] == 2, rec)
    check("keeps access_hash", rec["contacts"][0].get("access_hash") == "999",
          rec["contacts"][0])
    check("omits access_hash when absent",
          "access_hash" not in rec["contacts"][1], rec["contacts"][1])
    check("items() shape", contacts_store.items("acc") == [("Ali", "111"), ("NoHash", "222")],
          contacts_store.items("acc"))
    check("count()", contacts_store.count("acc") == 2)
    check("forget()", contacts_store.forget("acc") and contacts_store.count("acc") == 0)


# ---- resume ledger -----------------------------------------------------

def test_progress_store() -> None:
    print("progress_store (resume)")
    c1 = {"kind": "file", "file_path": "/a/b.zip", "file_name": "b.zip", "caption": "hi"}
    c2 = dict(c1, caption="changed")
    c3 = {"kind": "text", "text": "hello"}
    k1, k2, k3 = (progress_store.content_key(x) for x in (c1, c2, c3))
    check("key is stable", k1 == progress_store.content_key(dict(c1)))
    check("caption change -> new key", k1 != k2)
    check("kind is in the key", k3.startswith("text:") and k1.startswith("file:"))

    led = progress_store.open_ledger("acc", k1)
    for i in range(30):
        led.mark(f"n{i}", str(1000 + i))
    led.flush()
    check("marks persist", progress_store.done_count("acc", k1) == 30,
          progress_store.done_count("acc", k1))

    again = progress_store.open_ledger("acc", k1)
    check("reload restores ledger", len(again.done) == 30)
    check("has() true for delivered", again.has("n0", "1000"))
    check("has() false for new", not again.has("nX", "9999"))

    items = [(f"n{i}", str(1000 + i)) for i in range(35)]
    left = [(n, p) for n, p in items if not again.has(n, p)]
    check("resume leaves only the remainder", len(left) == 5, left)

    fresh = progress_store.open_ledger("acc", k2)
    check("different content -> empty ledger", len(fresh.done) == 0)

    check("targets without peer_id use the title",
          progress_store.target_key("Ali", None) == "title:Ali")
    check("clear() wipes", progress_store.clear("acc")
          and progress_store.done_count("acc", k1) == 0)


# ---- flood wait --------------------------------------------------------

def test_flood_wait() -> None:
    print("flood wait parsing")
    check("FLOOD_WAIT_23", _flood_wait("FLOOD_WAIT_23") == 23)
    check("lowercase", _flood_wait("flood_wait_5") == 5)
    check("embedded", _flood_wait("rpc error FLOOD_WAIT_120 on sendMedia") == 120)
    check("explicit wait field wins", _flood_wait("FLOOD_WAIT_9", 42) == 42)
    check("no number -> None", _flood_wait("PEER_FLOOD") is None)
    check("None -> None", _flood_wait(None) is None)
    check("under the cap is honoured", (_flood_wait("FLOOD_WAIT_30") or 0) <= config.MAX_FLOOD_WAIT)
    check("over the cap stops the run",
          (_flood_wait("FLOOD_WAIT_3600") or 0) > config.MAX_FLOOD_WAIT)


def test_limit_detection() -> None:
    print("limit detection")
    check("english phrase", _is_limit("too many requests, slow down"))
    check("persian phrase", _is_limit("حساب شما محدود شده است"))
    check("normal failure is not a limit", not _is_limit("element not found"))


# ---- guards ------------------------------------------------------------

def test_guards() -> None:
    print("guards")
    check("failure brake is above a short rough patch", _FAILURE_BRAKE >= 15,
          _FAILURE_BRAKE)
    check("brake is not unlimited", _FAILURE_BRAKE <= 50)
    check("file re-init is bounded", 1 <= _MAX_FILE_REINITS <= 5)
    check("concurrency default is the proven sequential path",
          config.SEND_CONCURRENCY == 1, config.SEND_CONCURRENCY)
    # These four belong to the CLI campaign runner. They must still exist
    # (jobs/campaign.py reads them at runtime) but the panel must NOT use them.
    check("CLI campaign knobs still exist",
          all(hasattr(config, n) for n in
              ("SEND_MIN_DELAY", "SEND_MAX_DELAY", "SEND_BATCH_SIZE",
               "SEND_BATCH_COOLDOWN")))
    import pathlib
    runner_src = pathlib.Path(__file__).resolve().parents[1].joinpath("runner.py").read_text()
    check("the panel's send loop does not read the CLI knobs",
          not any(n in runner_src for n in
                  ("SEND_MIN_DELAY", "SEND_MAX_DELAY", "SEND_BATCH_SIZE",
                   "SEND_BATCH_COOLDOWN")))
    camp = pathlib.Path(__file__).resolve().parents[2].joinpath("jobs/campaign.py")
    if camp.is_file():
        check("the CLI campaign still resolves its knobs",
              all(getattr(config, n) is not None for n in
                  ("SEND_MIN_DELAY", "SEND_BATCH_COOLDOWN")))


# ---- store -------------------------------------------------------------

def test_store() -> None:
    print("store")
    from bot.store import Store
    st = Store()
    check("concurrency clamps low", True)
    st.set_setting("send_concurrency", 0)
    check("0 -> 1", st.send_concurrency == 1, st.send_concurrency)
    st.set_setting("send_concurrency", 99)
    check("99 -> 10", st.send_concurrency == 10, st.send_concurrency)
    st.set_setting("send_concurrency", 3)
    check("3 stays 3", st.send_concurrency == 3)
    check("settings dict carries concurrency",
          st.settings.get("send_concurrency") == 3)

    check("no last run yet", st.last_run == {})
    st.set_last_run(account="98912", kind="File", sent=502, failed=2, skipped=0,
                    total=1094, elapsed=2481.0, stopped=False)
    lr = st.last_run
    check("last run persists", lr.get("sent") == 502 and lr.get("failed") == 2, lr)
    check("last run is timestamped", isinstance(lr.get("at"), float))

    st.set_account_meta("acc", contacts=1094, pvs=20)
    m = st.account_meta("acc")
    check("meta is timestamped", isinstance(m.get("meta_updated"), float), m)
    st.set_account_meta("acc", phone="98912")
    check("a phone-only update does not restamp",
          st.account_meta("acc").get("meta_updated") == m.get("meta_updated"))


# ---- cards -------------------------------------------------------------

def test_cards() -> None:
    print("cards")
    row = cards._rows([("Phone   ", "98912"), ("Empty", None)])
    check("label padding is stripped", row == ["• Phone: 98912"], row)
    check("None rows are dropped", len(row) == 1)

    home = cards.panel_home(10, 7, "98912", ping_ms=563, contacts=1094,
                            running=0, content="File — x.zip",
                            last_run={"sent": 502, "failed": 2, "elapsed": 2481.0,
                                      "at": __import__("time").time()})
    check("home has no version row", "Version" not in home, home)
    check("home has no bot-online row", "online" not in home)
    check("home has no progress bar", "▰" not in home and "%" not in home)
    check("home splits ready accounts", "7 ready" in home, home)
    check("home shows sendable contacts", "1,094 sendable" in home)
    check("home shows the last run", "502 sent" in home and "2 failed" in home)
    check("home keeps the API/ping row", "563 ms" in home)

    empty = cards.panel_home(0, 0, None)
    check("empty home is honest", "none yet" in empty and "nothing set" in empty, empty)

    panel = cards.account_panel("acc", "98912", 1414, 20, "bridge", False,
                                saved=1094, saved_age=0.2, meta_age=50.0, pending=300)
    check("account panel dates the Eitaa number", "measured" in panel, panel)
    check("account panel shows already-sent", "300 got the current content" in panel)

    err = cards.error_card("send", "acc", code="X", detail="token: abc123 leaked")
    check("secrets are redacted in cards", "abc123" not in err, err)


def main() -> int:
    for fn in (test_contacts_store, test_progress_store, test_flood_wait,
               test_limit_detection, test_guards, test_store, test_cards):
        fn()
    print()
    print(f"{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("ALL BOT TESTS PASSED" if code == 0 else "BOT TESTS FAILED")
    sys.exit(code)
