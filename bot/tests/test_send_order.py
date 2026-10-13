"""Tests for the broadcast send order (eitaa/send_order.py).

Run: python -m bot.tests.test_send_order

No browser and no account: the tiering is a pure function of what Eitaa reports,
which is the reason it was written as one.

These tests exist because the PREVIOUS tiering looked fine and was wrong. It
aged `was_online = 0` against now, got ~56 years, and put 36 phantom contacts in
a "seen over a month ago" tier ABOVE the genuinely-recent ones. Nothing crashed
and the report looked plausible. So the sentinel rule, the tier ORDER, and the
24h-cutoff premise each get a test that fails loudly if reintroduced -- and each
was verified by reverting the fix and confirming the test goes red.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mkwl_sendorder_test_")
os.environ.setdefault("DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("ARTIFACTS_DIR", os.path.join(_TMP, "artifacts"))

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from eitaa.send_order import (  # noqa: E402
    COARSE_WINDOW_SEC,
    EXACT_WINDOW_SEC,
    TIER_KEYS,
    build_order,
    classify,
    groups,
)

FAILURES: list[str] = []
PASSED = 0

NOW = 1_800_000_000          # a fixed clock; nothing here may depend on today
HOUR, DAY = 3600, 86400


def check(what: str, ok: bool, detail=None) -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {what}")
    else:
        FAILURES.append(what)
        print(f"  FAIL {what}" + (f"   -> {detail!r}" if detail is not None else ""))


def c(pid, status, was_online=None, expires=None, title=None):
    e = {"peer_id": str(pid), "access_hash": f"h{pid}",
         "title": title or f"c{pid}", "status": status}
    if was_online is not None:
        e["was_online"] = was_online
    if expires is not None:
        e["expires"] = expires
    return e


# --------------------------------------------------------------------------
# 1. Every status Eitaa is known to send lands in the intended tier.
# --------------------------------------------------------------------------
def test_classify_each_status() -> None:
    print("\n[classify] one status at a time")
    cases = [
        ("userStatusOnline",   dict(expires=NOW + 300),        "online",        "online"),
        ("userStatusOffline",  dict(was_online=NOW - 600),     "today",         "exact_within_24h"),
        ("userStatusOffline",  dict(was_online=NOW - 23 * HOUR), "today",       "exact_within_24h"),
        ("userStatusRecently", {},                             "recently",      "recently"),
        ("userStatusLastWeek", {},                             "week_or_month", "last_week"),
        ("userStatusLastMonth", {},                            "week_or_month", "last_month"),
        ("userStatusEmpty",    {},                             "long_ago",      "empty"),
    ]
    for status, kw, want_tier, want_reason in cases:
        tier, reason = classify(status, now=NOW, **kw)
        check(f"{status} -> {want_tier}", (tier, reason) == (want_tier, want_reason),
              (tier, reason))


# --------------------------------------------------------------------------
# 2. THE BUG. was_online = 0 must never be read as a date.
# --------------------------------------------------------------------------
def test_sentinel_zero_is_not_a_date() -> None:
    print("\n[sentinel] was_online == 0 is 'no time given', not 1970")
    tier, reason = classify("userStatusOffline", was_online=0, now=NOW)
    check("zero -> long_ago", tier == "long_ago", tier)
    check("zero is labelled sentinel_zero, not aged", reason == "sentinel_zero", reason)

    # The old code's actual failure mode: aged, then filed as very old. Both of
    # those are wrong, and for opposite reasons, so both are asserted.
    check("zero is NOT filed as a stale exact timestamp", reason != "exact_stale", reason)
    check("zero does NOT reach an active tier", tier not in ("online", "today"), tier)

    # And it must not outrank someone Eitaa says is recent -- that inversion is
    # what made the original defect harmful rather than merely untidy.
    plan = build_order([c(1, "userStatusOffline", was_online=0),
                        c(2, "userStatusRecently")], now=NOW)
    order = [e["peer_id"] for e in plan["ordered"]]
    check("a 'recently' contact is sent BEFORE a zero-sentinel one",
          order == ["2", "1"], order)


# --------------------------------------------------------------------------
# 3. The tier order is the order the user asked for, and sorting honours it.
# --------------------------------------------------------------------------
def test_tier_order_is_the_send_order() -> None:
    print("\n[order] online -> today -> recently -> week_or_month -> long_ago")
    check("tier list is exactly the five agreed tiers",
          TIER_KEYS == ["online", "today", "recently", "week_or_month", "long_ago"],
          TIER_KEYS)

    # Fed in DELIBERATELY BACKWARDS, so a no-op sort cannot pass this.
    scrambled = [
        c("E", "userStatusEmpty"),
        c("D", "userStatusLastMonth"),
        c("D2", "userStatusLastWeek"),
        c("C", "userStatusRecently"),
        c("B", "userStatusOffline", was_online=NOW - 2 * HOUR),
        c("A", "userStatusOnline", expires=NOW + 300),
    ]
    plan = build_order(scrambled, now=NOW)
    tiers = [e["tier"] for e in plan["ordered"]]
    check("sorted into tier order regardless of input order",
          tiers == ["online", "today", "recently", "week_or_month",
                    "week_or_month", "long_ago"], tiers)
    check("input was not mutated", "tier" not in scrambled[0], scrambled[0])

    # last_week and last_month share a tier, and neither is ranked above the
    # other, because there is no timestamp behind either to justify it.
    wm = [e["peer_id"] for e in plan["ordered"] if e["tier"] == "week_or_month"]
    check("week and month keep their incoming order (no invented precision)",
          wm == ["D", "D2"], wm)


def test_within_today_most_recent_first() -> None:
    print("\n[order] inside 'today', the exact timestamp IS used")
    plan = build_order([
        c("old", "userStatusOffline", was_online=NOW - 20 * HOUR),
        c("new", "userStatusOffline", was_online=NOW - 60),
        c("mid", "userStatusOffline", was_online=NOW - 5 * HOUR),
    ], now=NOW)
    order = [e["peer_id"] for e in plan["ordered"]]
    check("most recently seen first", order == ["new", "mid", "old"], order)


def test_order_is_deterministic() -> None:
    print("\n[order] same input -> same order, twice")
    rows = [c(i, "userStatusRecently") for i in range(20)]
    a = [e["peer_id"] for e in build_order(rows, now=NOW)["ordered"]]
    b = [e["peer_id"] for e in build_order(rows, now=NOW)["ordered"]]
    check("stable across runs", a == b)
    check("timeless tier keeps API order", a == [str(i) for i in range(20)], a[:5])


# --------------------------------------------------------------------------
# 4. The live census, reproduced exactly. This is the regression anchor.
# --------------------------------------------------------------------------
def test_reproduces_the_live_census() -> None:
    print("\n[live] the real 1,006-contact account, tier for tier")
    rows: list[dict] = []
    n = 0

    def add(k, status, **kw):
        nonlocal n
        for _ in range(k):
            n += 1
            rows.append(c(n, status, **kw))

    add(9, "userStatusOnline", expires=NOW + 300)              # online
    for i in range(55):                                        # seen_1h
        n += 1
        rows.append(c(n, "userStatusOffline", was_online=NOW - 600 - i))
    for i in range(154):                                       # seen_today
        n += 1
        rows.append(c(n, "userStatusOffline", was_online=NOW - 2 * HOUR - i * 300))
    add(36, "userStatusOffline", was_online=0)                  # the phantom tier
    add(76, "userStatusRecently")
    add(163, "userStatusLastWeek")
    add(253, "userStatusLastMonth")
    add(260, "userStatusEmpty")

    check("the fixture is the same size as the real account", len(rows) == 1006,
          len(rows))

    plan = build_order(rows, now=NOW, presence_age=30)
    tc = plan["tier_counts"]
    expect = {"online": 9, "today": 209, "recently": 76,
              "week_or_month": 416, "long_ago": 296}
    for key, want in expect.items():
        check(f"{key} == {want}", tc[key] == want, tc[key])

    check("every contact is placed exactly once",
          sum(tc.values()) == 1006 == plan["total"], (tc, plan["total"]))
    check("nothing was dropped", plan["dropped_no_peer"] == 0)

    # The 36 are inside long_ago but still individually identifiable, so the
    # distinction survives even though it does not drive the order.
    check("the 36 sentinels are still distinguishable from the 260 empties",
          plan["reason_counts"].get("sentinel_zero") == 36
          and plan["reason_counts"].get("empty") == 260,
          plan["reason_counts"])

    check("a healthy account raises no warnings", plan["warnings"] == [],
          plan["warnings"])

    g3 = {x["group"]: x["count"] for x in groups(plan, 3)}
    check("3 groups: 294 active / 416 coarse / 296 unknown",
          g3 == {"active": 294, "coarse": 416, "unknown": 296}, g3)
    g2 = {x["group"]: x["count"] for x in groups(plan, 2)}
    check("2 groups: 294 active / 712 rest",
          g2 == {"active": 294, "rest": 712}, g2)


# --------------------------------------------------------------------------
# 5. Boundaries. The 24h cutoff is a measured premise, so it is pinned.
# --------------------------------------------------------------------------
def test_exact_window_boundary() -> None:
    print("\n[boundary] the 24h edge")
    check("exactly 24h old is still 'today'",
          classify("userStatusOffline", was_online=NOW - DAY, now=NOW)[0] == "today")
    check("one second past 24h leaves 'today'",
          classify("userStatusOffline", was_online=NOW - DAY - 1, now=NOW)[0]
          == "week_or_month")
    check("one second past 24h does NOT fall to long_ago",
          classify("userStatusOffline", was_online=NOW - DAY - 1, now=NOW)[0]
          != "long_ago")
    check("the window constant is 24h", EXACT_WINDOW_SEC == DAY, EXACT_WINDOW_SEC)
    check("the coarse band ends at 30d", COARSE_WINDOW_SEC == 30 * DAY,
          COARSE_WINDOW_SEC)
    check("30d exactly is still week_or_month",
          classify("userStatusOffline", was_online=NOW - 30 * DAY, now=NOW)[0]
          == "week_or_month")
    check("past 30d falls to long_ago",
          classify("userStatusOffline", was_online=NOW - 30 * DAY - 1, now=NOW)[0]
          == "long_ago")


def test_unknown_status_is_reported_not_swallowed() -> None:
    print("\n[premise] an unrecognised status is reported")
    tier, reason = classify("userStatusSomethingNew", now=NOW)
    check("sent last as a safe default", tier == "long_ago", tier)
    check("reason names the constructor",
          reason == "unknown:userStatusSomethingNew", reason)
    plan = build_order([c(1, "userStatusSomethingNew")], now=NOW)
    check("and it warns", any("unrecognised" in w for w in plan["warnings"]),
          plan["warnings"])


def test_online_is_never_demoted_by_our_clock() -> None:
    """The live run emptied tier 1 completely. This is why.

    All 9 online contacts came back with an `expires` already in the past
    according to THIS host's clock, and an earlier version demoted every one of
    them to tier 2. The tier the whole feature exists for had zero members while
    the report looked healthy. Eitaa saying userStatusOnline is the statement
    that they are online; our clock does not get to overrule it.
    """
    print("\n[online] a past `expires` must NOT empty tier 1")
    tier, reason = classify("userStatusOnline", expires=NOW - 600, now=NOW)
    check("expires 10m in the past still means online",
          (tier, reason) == ("online", "online"), (tier, reason))
    check("and it is NOT relabelled online_expired", reason != "online_expired",
          reason)
    check("no expires at all still means online",
          classify("userStatusOnline", now=NOW)[0] == "online")
    check("future expires means online",
          classify("userStatusOnline", expires=NOW + 300, now=NOW)[0] == "online")

    # Reproduce the live shape: 9 online, every expires in the past.
    plan = build_order([c(i, "userStatusOnline", expires=NOW - 300 - i)
                        for i in range(9)], now=NOW, presence_age=30,
                       presence_source="users.getUsers (live)")
    check("all 9 land in tier 1", plan["tier_counts"]["online"] == 9,
          plan["tier_counts"])
    check("tier 2 stays empty", plan["tier_counts"]["today"] == 0)

    # Silently trusting the server is not the same as not noticing. The skew is
    # real information about the host and has to be surfaced.
    check("the clock skew is counted",
          plan["clock"]["online_expires_in_past"] == 9, plan["clock"])
    check("and reported as an observation, not a warning",
          any("stale" in o.lower() for o in plan["observations"])
          and plan["warnings"] == [],
          (plan["observations"], plan["warnings"]))


def test_boundary_drift_is_not_an_alarm() -> None:
    """One contact aged past 24h between two runs 65 minutes apart.

    That is ordinary drift, but it was originally reported as a premise-breaking
    WARNING and filed under long_ago, whose label reads "no usable last-seen
    signal" -- false for a contact whose exact last-seen time we know.
    """
    print("\n[drift] just past 24h is normal, and is not 'no signal'")
    tier, reason = classify("userStatusOffline", was_online=NOW - DAY - 300,
                            now=NOW)
    check("25h ago is not long_ago", tier == "week_or_month", tier)
    check("labelled exact_over_24h", reason == "exact_over_24h", reason)

    plan = build_order([c(1, "userStatusOffline", was_online=NOW - DAY - 300)],
                       now=NOW)
    check("it raises NO warning", plan["warnings"] == [], plan["warnings"])
    check("but it IS reported as an observation",
          any("24h edge" in o for o in plan["observations"]), plan["observations"])

    # It must never outrank someone with fresher proof.
    plan = build_order([c("drift", "userStatusOffline", was_online=NOW - DAY - 300),
                        c("fresh", "userStatusOffline", was_online=NOW - 60),
                        c("recent", "userStatusRecently")], now=NOW)
    check("ranked below both 'today' and 'recently'",
          [e["peer_id"] for e in plan["ordered"]] == ["fresh", "recent", "drift"],
          [e["peer_id"] for e in plan["ordered"]])

    # Far past the window is the reading that WOULD mean the premise broke.
    tier, reason = classify("userStatusOffline", was_online=NOW - 200 * DAY,
                            now=NOW)
    check("200 days with an exact time -> long_ago", tier == "long_ago", tier)
    check("and labelled exact_very_old", reason == "exact_very_old", reason)
    plan = build_order([c(1, "userStatusOffline", was_online=NOW - 200 * DAY)],
                       now=NOW)
    check("THAT one warns",
          any("premise" in w for w in plan["warnings"]), plan["warnings"])


def test_reproduces_the_live_build_run() -> None:
    """The second live run, exactly: online expired, one boundary drifter."""
    print("\n[live] the real build_send_order run, tier for tier")
    rows: list[dict] = []
    n = 0

    def add(k, status, **kw):
        nonlocal n
        for _ in range(k):
            n += 1
            rows.append(c(n, status, **kw))

    add(9, "userStatusOnline", expires=NOW - 400)        # expires in the past
    for i in range(208):
        n += 1
        rows.append(c(n, "userStatusOffline", was_online=NOW - 600 - i * 300))
    add(1, "userStatusOffline", was_online=NOW - DAY - 900)   # the drifter
    add(36, "userStatusOffline", was_online=0)
    add(76, "userStatusRecently")
    add(163, "userStatusLastWeek")
    add(253, "userStatusLastMonth")
    add(260, "userStatusEmpty")
    check("same size as the real account", len(rows) == 1006, len(rows))

    plan = build_order(rows, now=NOW, presence_age=30)
    tc = plan["tier_counts"]
    expect = {"online": 9, "today": 208, "recently": 76,
              "week_or_month": 417, "long_ago": 296}
    for key, want in expect.items():
        check(f"{key} == {want}", tc[key] == want, tc[key])
    check("every contact placed exactly once", sum(tc.values()) == 1006)
    check("no warnings on this data", plan["warnings"] == [], plan["warnings"])
    check("two observations: clock skew and boundary drift",
          len(plan["observations"]) == 2, plan["observations"])


def test_tier1_must_earn_its_name() -> None:
    """Tier 1 is the only tier that claims a MOMENT, so it must prove freshness.

    contacts.getContacts was measured returning presence frozen at session start
    for 105 minutes while every report looked healthy. The label is therefore
    derived from a measured age, and an unproven age gets the degraded label
    rather than the benefit of the doubt.
    """
    print("\n[tier1] the name 'online right now' has to be earned")
    rows = [c(i, "userStatusOnline", expires=NOW + 300) for i in range(9)]

    fresh = build_order(rows, now=NOW, presence_age=30,
                        presence_source="users.getUsers (live)")
    check("30s old -> verified live", fresh["presence"]["live"] is True,
          fresh["presence"])
    check("label says verified",
          "verified live" in fresh["presence"]["tier1_label"],
          fresh["presence"]["tier1_label"])
    check("and no warning", fresh["warnings"] == [], fresh["warnings"])
    check("the source is recorded",
          fresh["presence"]["source"] == "users.getUsers (live)")

    stale = build_order(rows, now=NOW, presence_age=105 * 60)
    check("105m old -> NOT live", stale["presence"]["live"] is False,
          stale["presence"])
    check("label states the actual age",
          "1h 45m" in stale["presence"]["tier1_label"],
          stale["presence"]["tier1_label"])
    check("does NOT claim 'right now'",
          "right now" not in stale["presence"]["tier1_label"],
          stale["presence"]["tier1_label"])
    check("and it WARNS, not merely observes",
          any("cannot be called" in w for w in stale["warnings"]),
          stale["warnings"])
    check("the warning names the live bridge",
          any("presence.js" in w for w in stale["warnings"]), stale["warnings"])

    unknown = build_order(rows, now=NOW)
    check("no age given -> not live (no benefit of the doubt)",
          unknown["presence"]["live"] is False, unknown["presence"])
    check("label says freshness is UNKNOWN",
          "UNKNOWN" in unknown["presence"]["tier1_label"],
          unknown["presence"]["tier1_label"])
    check("and it warns", unknown["warnings"] != [], unknown["warnings"])

    # The boundary, and the case where the warning would be noise.
    check("exactly 5m old still counts as live",
          build_order(rows, now=NOW, presence_age=300)["presence"]["live"] is True)
    check("5m and one second does not",
          build_order(rows, now=NOW, presence_age=301)["presence"]["live"] is False)

    # Coverage: a fresh age proves only that SOMETHING was refreshed.
    thin = build_order(rows, now=NOW, presence_age=30, presence_coverage_pct=10.0)
    check("fresh age but 10% coverage is NOT live", thin["presence"]["live"] is False,
          thin["presence"])
    check("the label says why", "10.0% of statuses" in thin["presence"]["tier1_label"],
          thin["presence"]["tier1_label"])
    check("and the warning names coverage",
          any("refreshed" in w for w in thin["warnings"]), thin["warnings"])
    check("99.4% coverage IS live",
          build_order(rows, now=NOW, presence_age=30,
                      presence_coverage_pct=99.4)["presence"]["live"] is True)
    check("exactly 90% is live",
          build_order(rows, now=NOW, presence_age=30,
                      presence_coverage_pct=90.0)["presence"]["live"] is True)
    check("89.9% is not",
          build_order(rows, now=NOW, presence_age=30,
                      presence_coverage_pct=89.9)["presence"]["live"] is False)
    check("unknown coverage does not block a fresh age",
          build_order(rows, now=NOW, presence_age=30,
                      presence_coverage_pct=None)["presence"]["live"] is True)
    empty = build_order([c(1, "userStatusRecently")], now=NOW)
    check("with an EMPTY tier 1 there is nothing to warn about",
          empty["warnings"] == [], empty["warnings"])


def test_edge_inputs() -> None:
    print("\n[edges] missing and malformed data")
    check("no status at all -> long_ago",
          classify("", now=NOW) == ("long_ago", "no_status"))
    check("None status -> long_ago",
          classify(None, now=NOW) == ("long_ago", "no_status"))
    check("offline with no timestamp -> long_ago",
          classify("userStatusOffline", now=NOW) == ("long_ago", "offline_no_time"))

    # A future timestamp is clock skew between Eitaa's clock and ours, not a
    # contact seen tomorrow. It must not be filed as long_ago.
    tier, reason = classify("userStatusOffline", was_online=NOW + 120, now=NOW)
    check("future timestamp -> today (clock skew)",
          (tier, reason) == ("today", "clock_skew"), (tier, reason))

    plan = build_order([], now=NOW)
    check("empty input is fine", plan["total"] == 0 and plan["warnings"] == []
          and plan["observations"] == [])

    plan = build_order([{"title": "no peer id", "status": "userStatusRecently"},
                        None, c(7, "userStatusRecently")], now=NOW)
    check("unaddressable rows are excluded from the tiers", plan["total"] == 1,
          plan["total"])
    check("and counted, not silently dropped", plan["dropped_no_peer"] == 2,
          plan["dropped_no_peer"])
    check("with a warning naming them",
          any("peer_id" in w for w in plan["warnings"]), plan["warnings"])


def test_fallback_reason_reaches_the_report() -> None:
    """A tier-1 warning with no stated cause is not actionable.

    The first fallback run printed why the live bridge failed BEFORE the report,
    where it scrolled out of view. What survived was a warning saying tier 1
    could not be trusted, with nothing saying why -- so the report now repeats
    the reason inside itself.
    """
    print("\n[report] the live-bridge failure must survive into the report")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bso", os.path.join(_ROOT, "deploy", "build_send_order.py"))
    bso = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bso)

    rows = [c(i, "userStatusOnline", expires=NOW - 6000) for i in range(9)]
    plan = build_order(rows, now=NOW)

    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        bso.report(plan, {"total": 6.9}, raw=1006, skipped=0, source="snapshot",
                   live_error={"code": "no contact ids: store is empty",
                               "errors": ["seed:CONNECTION_NOT_INITED"],
                               "connected": False, "store_ids": 0})
    finally:
        sys.stdout = old
    out = buf.getvalue()
    check("names the source used", "SOURCE: snapshot" in out, out[:300])
    check("states the reason", "store is empty" in out, out[:300])
    check("includes the underlying error", "CONNECTION_NOT_INITED" in out,
          out[:300])
    check("includes the evidence fields", "connected" in out and "store_ids" in out,
          out[:400])
    check("and still warns about tier 1",
          any("cannot be called" in w for w in plan["warnings"]))

    # A missing reason must be called out, not left blank.
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        bso.report(plan, {"total": 1.0}, raw=9, skipped=0, source="snapshot",
                   live_error=None)
    finally:
        sys.stdout = old
    check("an unrecorded reason is flagged as a bug, not left silent",
          "not recorded" in buf.getvalue(), buf.getvalue()[:300])

    # The live path says so plainly, so the two cannot be confused.
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        bso.report(build_order(rows, now=NOW, presence_age=30),
                   {"total": 1.0}, raw=9, skipped=0, source="live")
    finally:
        sys.stdout = old
    out = buf.getvalue()
    check("the live path is labelled as such",
          "SOURCE: live presence bridge" in out, out[:300])
    check("and carries no failure text", "did not run" not in out, out[:300])


def main() -> int:
    print("=" * 66)
    print("SEND ORDER — built on Eitaa's own reported behaviour")
    print("=" * 66)
    test_classify_each_status()
    test_sentinel_zero_is_not_a_date()
    test_tier_order_is_the_send_order()
    test_within_today_most_recent_first()
    test_order_is_deterministic()
    test_reproduces_the_live_census()
    test_exact_window_boundary()
    test_online_is_never_demoted_by_our_clock()
    test_boundary_drift_is_not_an_alarm()
    test_reproduces_the_live_build_run()
    test_tier1_must_earn_its_name()
    test_fallback_reason_reaches_the_report()
    test_unknown_status_is_reported_not_swallowed()
    test_edge_inputs()

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
