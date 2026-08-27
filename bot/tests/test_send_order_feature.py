"""The Send Order feature must be unable to harm a send. Run:

    python -m bot.tests.test_send_order_feature

The tiering logic itself is covered by test_send_order.py. This file tests only
the WIRING, because that is where an opt-in feature does damage: the requirement
was "if it breaks, it must not matter". So every way it can break is exercised
and each must leave the original order intact and say why.

No browser: the driver is a stub returning whatever shape a test needs.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mkwl_so_feature_")
os.environ.setdefault("DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("ARTIFACTS_DIR", os.path.join(_TMP, "artifacts"))
os.environ.setdefault("PROFILES_DIR", os.path.join(_TMP, "profiles"))

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _stub(mod: str, names=(), **attrs) -> None:
    """Stub a module that is not installed here. jobs.campaign imports the real
    browser stack transitively, and none of it is reachable in this test."""
    import types
    if mod in sys.modules:
        return
    m = types.ModuleType(mod)
    for n in names:
        setattr(m, n, type(n, (object,), {}))
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[mod] = m
    if "." in mod:
        parent, child = mod.rsplit(".", 1)
        if parent in sys.modules:
            setattr(sys.modules[parent], child, m)


try:
    import playwright.async_api  # noqa: F401
except Exception:  # noqa: BLE001
    _stub("playwright")
    _stub("playwright.async_api",
          ("BrowserContext", "CDPSession", "Page", "Locator", "Error"),
          TimeoutError=type("TimeoutError", (Exception,), {}),
          async_playwright=lambda: None)

FAILURES: list[str] = []
PASSED = 0
NOW = 1_800_000_000


def check(what: str, ok: bool, detail=None) -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {what}")
    else:
        FAILURES.append(what)
        print(f"  FAIL {what}" + (f"   -> {detail!r}" if detail is not None else ""))


class R:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


CONTACTS = [
    {"peer_id": "1", "title": "ALICE", "status": "userStatusEmpty"},
    {"peer_id": "2", "title": "BOB", "status": "userStatusOnline",
     "expires": NOW + 300},
    {"peer_id": "3", "title": "CARA", "status": "userStatusRecently"},
    {"peer_id": "4", "title": "DAN", "status": "userStatusOffline",
     "was_online": NOW - 600},
    {"peer_id": "5", "title": "EVE", "status": "userStatusLastMonth"},
]
NAMES = ["ALICE", "BOB", "CARA", "DAN", "EVE"]
WANT = ["BOB", "DAN", "CARA", "EVE", "ALICE"]


class Driver:
    """Stub driver. `reply` is whatever bridge_contacts_list should return."""

    def __init__(self, reply):
        self._reply = reply
        self.calls = 0

    async def bridge_contacts_list(self):
        self.calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def run(driver, recipients):
    """Call the real _apply_send_order, capturing what it printed."""
    from jobs.campaign import _apply_send_order
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        out = asyncio.run(_apply_send_order(driver, recipients))
    finally:
        sys.stdout = old
    return out, buf.getvalue()


def set_toggle(on: bool) -> None:
    from bot.store import store
    store.set_setting("send_order", on)


def test_off_is_a_true_no_op() -> None:
    """OFF must not merely restore the old order -- it must never leave it."""
    print("\n[off] the default must change nothing at all")
    set_toggle(False)
    recips = [R(n) for n in NAMES]
    drv = Driver({"ok": True, "contacts": CONTACTS, "server_now": NOW})
    out, log = run(drv, recips)
    check("order is byte-for-byte the input", [r.name for r in out] == NAMES,
          [r.name for r in out])
    check("the same list object contents, nothing dropped", len(out) == len(recips))
    check("the contacts bridge is NEVER called when off", drv.calls == 0, drv.calls)
    check("and it prints nothing", log == "", log)


def test_on_applies_the_agreed_order() -> None:
    print("\n[on] tiers 1->5, through to the last contact")
    set_toggle(True)
    recips = [R(n) for n in NAMES]
    drv = Driver({"ok": True, "contacts": CONTACTS, "server_now": NOW})
    out, log = run(drv, recips)
    check("online, today, recently, week_or_month, long_ago",
          [r.name for r in out] == WANT, [r.name for r in out])
    check("nobody is dropped", sorted(r.name for r in out) == sorted(NAMES))
    check("it logs what it did", "[send_order] applied:" in log, log)
    check("the log names every tier",
          all(k in log for k in ("online=", "today=", "recently=",
                                 "week_or_month=", "long_ago=")), log)


def test_recipients_with_no_status_go_last_not_first() -> None:
    print("\n[on] an unknown tier is not evidence of activity")
    set_toggle(True)
    recips = [R("GHOST1"), R("BOB"), R("GHOST2")]
    drv = Driver({"ok": True, "contacts": CONTACTS, "server_now": NOW})
    out, log = run(drv, recips)
    check("the known-online one goes first", out[0].name == "BOB", out)
    check("unmatched keep their relative order, at the end",
          [r.name for r in out[1:]] == ["GHOST1", "GHOST2"], out)
    check("and they are counted, not hidden", "no-status=2" in log, log)


def test_every_failure_keeps_the_original_order() -> None:
    """The whole requirement: if it breaks, it must not matter."""
    print("\n[broken] each failure mode must be harmless AND explained")
    set_toggle(True)
    cases = [
        ("bridge raises", Driver(RuntimeError("page detached")), "FAILED"),
        ("bridge returns None", Driver(None), "SKIPPED"),
        ("bridge returns not-ok",
         Driver({"ok": False, "code": "getContacts:AUTH_KEY_INVALID"}), "SKIPPED"),
        ("contacts have NO status field",
         Driver({"ok": True, "contacts": [{"peer_id": "1", "title": "ALICE"}],
                 "server_now": NOW}), "SKIPPED"),
        ("contacts list is garbage",
         Driver({"ok": True, "contacts": [None, 5, "x"], "server_now": NOW}),
         "SKIPPED"),
        ("reply is not a dict", Driver("nonsense"), "SKIPPED"),
    ]
    for label, drv, expect in cases:
        recips = [R(n) for n in NAMES]
        out, log = run(drv, recips)
        check(f"{label}: original order kept",
              [r.name for r in out] == NAMES, [r.name for r in out])
        check(f"{label}: nobody lost", len(out) == len(NAMES), len(out))
        check(f"{label}: says {expect} with a reason",
              expect in log and "order kept" in log, log.strip())


def test_a_stale_cached_bridge_does_not_look_like_success() -> None:
    """No status on ANY contact would tier everyone into the bottom tier.

    That is the dangerous case: it would "work", report a full ordering, and
    silently send to the least active people in an arbitrary order.
    """
    print("\n[stale] a bridge with no status must refuse, not invent an order")
    set_toggle(True)
    recips = [R(n) for n in NAMES]
    drv = Driver({"ok": True, "server_now": NOW,
                  "contacts": [{"peer_id": str(i), "title": n}
                               for i, n in enumerate(NAMES, 1)]})
    out, log = run(drv, recips)
    check("order untouched", [r.name for r in out] == NAMES, [r.name for r in out])
    check("and it says the status field was missing",
          "no status field" in log, log.strip())
    check("naming the cause as a stale cached bridge",
          "stale cached bridge" in log, log.strip())


def test_login_card_rows() -> None:
    print("\n[card] the login card shows the three counts, or none")
    from bot import cards
    from eitaa.send_order import card_counts

    tiers = card_counts(CONTACTS, now=NOW)
    check("counts computed from the login contacts",
          tiers == {"online": 1, "today": 1, "recently": 1, "total": 5}, tiers)

    txt = cards.account_added("acc", "989991048633", 5, 3, "direct", saved=5,
                             tiers=tiers)
    for label in ("Online", "Today", "Recently"):
        check(f"card shows {label}", label in txt, txt)
    check("with a percentage", "20.0%" in txt, txt)
    check("contact count is still there", "Contacts" in txt, txt)

    plain = cards.account_added("acc", "989991048633", 5, 3, "direct", saved=5)
    check("without the feature the rows are absent", "Online" not in plain, plain)
    check("but the card still works", "ACCOUNT ADDED" in plain, plain)

    # The important one: never 0/0/0 when the data cannot say.
    check("no status anywhere -> no rows, not zeros",
          card_counts([{"peer_id": "1", "title": "X"}], now=NOW) is None)


def main() -> int:
    print("=" * 66)
    print("SEND ORDER FEATURE — opt-in, and unable to harm a send")
    print("=" * 66)
    test_off_is_a_true_no_op()
    test_on_applies_the_agreed_order()
    test_recipients_with_no_status_go_last_not_first()
    test_every_failure_keeps_the_original_order()
    test_a_stale_cached_bridge_does_not_look_like_success()
    test_login_card_rows()
    set_toggle(False)          # leave the box as we found it

    print("")
    print("=" * 66)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} of {PASSED + len(FAILURES)} checks")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"ALL {PASSED} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
