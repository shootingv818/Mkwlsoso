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
    """Call the real _apply_send_order (the CLI path), capturing its output."""
    from jobs.campaign import _apply_send_order
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        out = asyncio.run(_apply_send_order(driver, recipients))
    finally:
        sys.stdout = old
    return out, buf.getvalue()


def run_bot(account, items, driver=None):
    """Call the real _order_by_tier -- the path the BOT actually sends through.

    This exists because the feature was first wired ONLY into jobs/campaign.py,
    which is the CLI. The bot sends via bot/runner.py, so the ordering never ran
    and a live send came out in its original order while reporting success.
    """
    from bot.runner import _order_by_tier
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        res = asyncio.run(_order_by_tier(account, items, driver=driver))
    finally:
        sys.stdout = old
    return res, buf.getvalue()


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


def test_the_path_the_bot_actually_sends_through() -> None:
    """The regression that mattered: the bot's OWN loop must be ordered.

    Reported live on a 509-contact account -- 340 "recently" -- as a send that
    came out mixed, with month-old and week-old contacts interleaved. The cause
    was not the tiering: it was that the ordering had been wired into
    jobs/campaign.py (the CLI path) while the bot sends through bot/runner.py.
    The feature reported success and changed nothing.
    """
    print("\n[bot path] bot/runner.py is the loop that must be reordered")
    from bot import contacts_store as cs
    import random

    raw = []
    for i in range(340):
        raw.append({"title": f"R{i}", "peer_id": str(i), "access_hash": "h",
                    "status": "userStatusRecently"})
    for i in range(120):
        raw.append({"title": f"W{i}", "peer_id": str(1000 + i),
                    "access_hash": "h", "status": "userStatusLastWeek"})
    for i in range(49):
        raw.append({"title": f"E{i}", "peer_id": str(2000 + i),
                    "access_hash": "h", "status": "userStatusEmpty"})
    cs.save("ACC", raw)

    check("the tier is persisted with the contacts",
          cs.tier_counts("ACC") == {"recently": 340, "week_or_month": 120,
                                    "long_ago": 49}, cs.tier_counts("ACC"))
    check("and a rank map is available without a browser",
          len(cs.tiers("ACC")) == 509, len(cs.tiers("ACC")))

    items = cs.items("ACC")
    random.seed(1)
    random.shuffle(items)
    before = [n for n, _ in items]

    set_toggle(False)
    (out, tb, tt), log = run_bot("ACC", items)
    check("OFF: order is identical", [n for n, _ in out] == before)
    check("OFF: no tier maps, so no card is posted", (tb, tt) == ({}, {}))
    check("OFF: silent", log == "", log)

    set_toggle(True)
    (out, tb, tt), log = run_bot("ACC", items)
    names = [n for n, _ in out]
    check("ON: the 340 'recently' go FIRST",
          all(n[0] == "R" for n in names[:340]), names[:3])
    check("ON: then the 120 week/month",
          all(n[0] == "W" for n in names[340:460]), names[340:343])
    check("ON: then the 49 with no signal",
          all(n[0] == "E" for n in names[460:]), names[460:463])
    check("ON: all 509 present, none duplicated",
          len(names) == 509 == len(set(names)), len(names))
    check("ON: it logs the applied tiers", "[send_order] applied to 509" in log,
          log)

    # The live card's stage line, which is what makes a long uniform stretch
    # readable instead of looking like the order was ignored.
    from bot.runner import _stage_for
    peers = [p for _n, p in out]

    def stage(k):
        return _stage_for(peers[k], names[k], tb, tt) or ""
    check("stage at the start names tier 3 of 5", "اخیراً (3 از 5)" in stage(0),
          stage(0))
    check("stage after 400 has moved to tier 4", "(4 از 5)" in stage(400),
          stage(400))
    check("stage counts the tier's size", "340" in stage(0), stage(0))

    # A cache written before tiers existed must be left completely alone.
    cs.save("OLD", [{"title": "X1", "peer_id": "1"},
                    {"title": "X2", "peer_id": "2"}])
    (out2, _tb2, tt2), log2 = run_bot("OLD", cs.items("OLD"))
    check("a tier-less cache is left untouched",
          [n for n, _ in out2] == ["X1", "X2"], out2)
    check("no card is posted for it", tt2 == {}, tt2)
    check("and it says to run Update Contacts",
          "Update Contacts" in log2, log2.strip())
    set_toggle(False)


def test_duplicate_titles_do_not_swap_tiers() -> None:
    """Reported live as an order that was "mostly right, a few wrong".

    Real contact lists contain the same name twice. The lookup was keyed on
    title, so one of the pair got the other's tier -- a handful of contacts sent
    in the wrong place while the rest looked fine, which is exactly the shape of
    the complaint. peer_id is unique and is now the primary key.
    """
    print("\n[duplicate names] peer_id must win over title")
    from bot import contacts_store as cs
    from bot.runner import _order_by_tier, _stage_for

    cs.save("DUP", [
        {"title": "علی", "peer_id": "1", "access_hash": "h",
         "status": "userStatusOnline", "expires": 9e9},
        {"title": "علی", "peer_id": "2", "access_hash": "h",
         "status": "userStatusLastMonth"},
        {"title": "زهرا", "peer_id": "3", "access_hash": "h",
         "status": "userStatusRecently"},
    ])
    check("each duplicate keeps its OWN tier in the cache",
          cs.tiers_by_peer("DUP") == {"1": 0, "2": 3, "3": 2},
          cs.tiers_by_peer("DUP"))

    set_toggle(True)
    # The month-old 'علی' is fed FIRST, so a title-keyed lookup would leave it
    # ahead of the online one.
    (out, maps, tt), log = run_bot("DUP", [("علی", "2"), ("زهرا", "3"),
                                           ("علی", "1")])
    check("the ONLINE علی goes first, not the month-old one",
          out == [("علی", "1"), ("زهرا", "3"), ("علی", "2")], out)
    check("every match was made by peer_id", "matched-by-peer=3" in log, log)
    check("and none fell back to title", "by-title=0" in log, log)
    check("the stage label agrees with the position for peer 1",
          "آنلاین" in (_stage_for("1", "علی", maps, tt) or ""),
          _stage_for("1", "علی", maps, tt))
    check("and differs for peer 2 despite the same name",
          "هفته/ماه" in (_stage_for("2", "علی", maps, tt) or ""),
          _stage_for("2", "علی", maps, tt))
    set_toggle(False)


def test_a_tierless_cache_repairs_itself() -> None:
    """The reason no stage line appeared on the live run.

    The account's cache predated tier saving, so there was nothing to order by.
    The old code skipped and told the owner to run "Update Contacts" -- a manual
    step nobody had reason to know about, in a log they were not reading. With a
    driver available the statuses are now read live and written to the cache.
    """
    print("\n[self-heal] a cache with no tiers must fix itself, not just skip")
    from bot import contacts_store as cs
    from bot.runner import _order_by_tier

    class LiveDriver:
        def __init__(self, reply):
            self._reply = reply
            self.calls = 0

        async def bridge_contacts_list(self):
            self.calls += 1
            if isinstance(self._reply, Exception):
                raise self._reply
            return self._reply

    cs.save("HEAL", [{"title": "X1", "peer_id": "11"},
                     {"title": "X2", "peer_id": "12"}])
    check("the cache starts with no tiers", cs.tiers_by_peer("HEAL") == {},
          cs.tiers_by_peer("HEAL"))

    set_toggle(True)
    drv = LiveDriver({"ok": True, "server_now": NOW, "contacts": [
        {"title": "X1", "peer_id": "11", "access_hash": "h",
         "status": "userStatusLastMonth"},
        {"title": "X2", "peer_id": "12", "access_hash": "h",
         "status": "userStatusOnline", "expires": 9e9}]})
    (out, maps, tt), log = run_bot("HEAL", cs.items("HEAL"), driver=drv)
    check("it read the statuses live", "read statuses live and saved 2" in log,
          log.strip())
    check("the order was then applied", out == [("X2", "12"), ("X1", "11")], out)
    check("and the tiers are PERSISTED for next time",
          cs.tiers_by_peer("HEAL") == {"11": 3, "12": 0},
          cs.tiers_by_peer("HEAL"))
    check("so the card can be posted", tt.get("online") == 1, tt)

    # A second send needs no repair: the cache is already annotated.
    drv2 = LiveDriver(RuntimeError("must not be called"))
    (out2, _m2, _t2), _log2 = run_bot("HEAL", cs.items("HEAL"), driver=drv2)
    check("a later send does not re-read", drv2.calls == 0, drv2.calls)
    check("and is still ordered", out2 == [("X2", "12"), ("X1", "11")], out2)

    # Repair failing must still leave the send alone rather than break it.
    cs.save("HEAL2", [{"title": "Y1", "peer_id": "21"}])
    before = cs.items("HEAL2")
    (out3, _m3, tt3), log3 = run_bot("HEAL2", before,
                                     driver=LiveDriver(RuntimeError("page gone")))
    check("a failed repair keeps the original order", out3 == before, out3)
    check("says the live read failed", "live tier read failed" in log3,
          log3.strip())
    check("and posts no card", tt3 == {}, tt3)

    # A bridge with no statuses (old cached JS) must say so, not invent tiers.
    (out4, _m4, tt4), log4 = run_bot(
        "HEAL2", before,
        driver=LiveDriver({"ok": True, "server_now": NOW,
                           "contacts": [{"title": "Y1", "peer_id": "21"}]}))
    check("a statusless bridge is reported", "returned no statuses" in log4,
          log4.strip())
    check("order untouched", out4 == before, out4)
    check("no card", tt4 == {}, tt4)
    set_toggle(False)


def main() -> int:
    print("=" * 66)
    print("SEND ORDER FEATURE — opt-in, and unable to harm a send")
    print("=" * 66)
    test_the_path_the_bot_actually_sends_through()
    test_duplicate_titles_do_not_swap_tiers()
    test_a_tierless_cache_repairs_itself()
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
