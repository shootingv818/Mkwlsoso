"""Tests for the freshness probe's verdict logic (deploy/status_freshness.py).

Run: python -m bot.tests.test_status_freshness

Only the verdict is tested, because only the verdict was wrong. Run 1 of the
probe printed:

    B users.getUsers (30)   FAILED: users.getUsers:PEER_ID_INVALID
    ...
    Every round agreed with A.

Three rounds agreed. The fourth errored. A round that ERRORED agrees with
nothing, and announcing consensus over the top of it turned a broken probe into
a confident false conclusion -- the exact failure mode this probe exists to
avoid in the first place.

No browser: the rounds are fed in as plain dicts, which is all the verdict ever
sees.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mkwl_fresh_test_")
os.environ.setdefault("DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("ARTIFACTS_DIR", os.path.join(_TMP, "artifacts"))

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location(
    "status_freshness", os.path.join(_ROOT, "deploy", "status_freshness.py"))
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)

FAILURES: list[str] = []
PASSED = 0

BASE = 1_800_000_000


def check(what: str, ok: bool, detail=None) -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {what}")
    else:
        FAILURES.append(what)
        print(f"  FAIL {what}" + (f"   -> {detail!r}" if detail is not None else ""))


def good(newest: int, seen: int = 1006) -> dict:
    return {"ok": True, "newest": newest, "newest_expires": newest + 500,
            "seen": seen, "ms": 400, "page_now": BASE + 5000}


def verdict_text(rounds: list[tuple[str, dict]]) -> str:
    """Run the verdict with stdout captured, so we assert on what the user reads."""
    R = sf.Rounds()
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        for tag, r in rounds:
            if r.get("skipped"):
                R.skip(tag, str(r.get("code")))
            else:
                R.add(tag, r)
        R.verdict()
    finally:
        sys.stdout = old
    return buf.getvalue()


def test_failure_is_never_reported_as_agreement() -> None:
    """The actual regression: run 1's exact shape."""
    print("\n[verdict] a FAILED round must break consensus")
    out = verdict_text([
        ("A warm getContacts", good(BASE)),
        ("E contacts.getStatuses", good(BASE)),
        ("B users.getUsers", {"ok": False, "code": "users.getUsers:PEER_ID_INVALID"}),
        ("C getContacts +45s", good(BASE)),
        ("D after reload", good(BASE)),
    ])
    check("does NOT claim every round agreed",
          "Every round succeeded" not in out, out[-260:])
    check("says no conclusion yet", "NO CONCLUSION YET" in out, out[-260:])
    check("names the unresolved round", "PEER_ID_INVALID" in out, out[-260:])
    check("does not declare a server snapshot",
          "really is" not in out, out[-260:])
    check("counts it as unresolved, not answered", "unresolved: 1" in out,
          out[-400:])
    check("and blames us, not Eitaa, for a PEER_ID_INVALID",
          "unresolved (our problem)" in out, out[-400:])


def test_clean_consensus_is_allowed() -> None:
    print("\n[verdict] all rounds ok and equal -> a real conclusion")
    out = verdict_text([
        ("A warm getContacts", good(BASE)),
        ("E contacts.getStatuses", good(BASE)),
        ("B users.getUsers", good(BASE)),
        ("C getContacts +45s", good(BASE)),
        ("D after reload", good(BASE)),
    ])
    check("declares the snapshot finding", "server-side snapshot" in out, out[-300:])
    check("tells us to relabel tier 1", "relabelled honestly" in out, out[-300:])
    check("reports zero unresolved", "unresolved: 0" in out, out[-400:])
    check("no false 'no conclusion'", "NO CONCLUSION YET" not in out)


def test_unsupported_method_is_an_answer_not_a_failure() -> None:
    """INVALID_CONSTRUCTOR means Eitaa does not have the method. That is a result.

    Lumping it in with our own broken calls would leave the probe permanently
    inconclusive over something no amount of fixing can change -- run 2 hit
    exactly this with contacts.getStatuses.
    """
    print("\n[verdict] 'Eitaa has no such method' must not block a conclusion")
    out = verdict_text([
        ("A warm getContacts", good(BASE)),
        ("E contacts.getStatuses",
         {"ok": False, "code": "contacts.getStatuses:INVALID_CONSTRUCTOR"}),
        ("C getContacts +45s", good(BASE)),
        ("G live store", good(BASE)),
    ])
    check("counted as unsupported", "unsupported by Eitaa: 1" in out, out[-400:])
    check("zero unresolved", "unresolved: 0" in out, out[-400:])
    check("labelled a real answer", "unsupported (a real answer)" in out,
          out[-400:])
    check("so a conclusion IS reached", "server-side snapshot" in out, out[-400:])
    check("and no false 'no conclusion'", "NO CONCLUSION YET" not in out)

    # But our own broken call still blocks it, even alongside an unsupported one.
    out = verdict_text([
        ("A warm getContacts", good(BASE)),
        ("E contacts.getStatuses",
         {"ok": False, "code": "contacts.getStatuses:INVALID_CONSTRUCTOR"}),
        ("B users.getUsers", {"ok": False, "code": "PEER_ID_INVALID"}),
        ("G live store", good(BASE)),
    ])
    check("a broken round still blocks the conclusion",
          "NO CONCLUSION YET" in out, out[-400:])
    check("and the two kinds are reported separately",
          "unsupported by Eitaa: 1" in out and "unresolved: 1" in out, out[-400:])


def test_a_newer_round_wins_even_with_a_failure() -> None:
    """Finding fresh data is actionable regardless of an unrelated failure."""
    print("\n[verdict] a fresher round is the answer, and outranks a failure")
    out = verdict_text([
        ("A warm getContacts", good(BASE)),
        ("E contacts.getStatuses", good(BASE + 3600)),
        ("B users.getUsers", {"ok": False, "code": "PEER_ID_INVALID"}),
        ("C getContacts +45s", good(BASE)),
    ])
    check("announces fresh data is reachable",
          "FRESH DATA IS REACHABLE" in out, out[-300:])
    check("names the winning round",
          "Round E contacts.getStatuses" in out, out[-300:])
    check("reports the gain in minutes", "60m" in out, out[-300:])
    check("still lists the unresolved round", "PEER_ID_INVALID" in out, out[-400:])


def test_skipped_counts_as_unresolved() -> None:
    print("\n[verdict] a SKIPPED round is unresolved too, not agreement")
    out = verdict_text([
        ("A warm getContacts", good(BASE)),
        ("B users.getUsers", {"skipped": True, "code": "no usable contact ids"}),
        ("C getContacts +45s", good(BASE)),
    ])
    check("no consensus claimed", "Every round succeeded" not in out)
    check("no conclusion", "NO CONCLUSION YET" in out, out[-260:])
    check("names why it was skipped", "no usable contact ids" in out, out[-300:])


def test_baseline_comes_from_the_first_successful_round() -> None:
    print("\n[verdict] a failed FIRST round must not become the baseline")
    R = sf.Rounds()
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        R.add("A warm getContacts", {"ok": False, "code": "no invokeApi"})
        R.add("E contacts.getStatuses", good(BASE))
    finally:
        sys.stdout = old
    check("baseline taken from the round that worked", R.base == BASE, R.base)

    # And a malformed reply must not crash or be mistaken for success.
    R2 = sf.Rounds()
    sys.stdout = io.StringIO()
    try:
        R2.add("A", None)
    finally:
        sys.stdout = old
    check("a non-dict reply is recorded as failed",
          R2.rows[0][1]["ok"] is False, R2.rows[0])
    check("and does not set a baseline", R2.base is None, R2.base)


def main() -> int:
    print("=" * 66)
    print("STATUS FRESHNESS — the verdict must not read a failure as a result")
    print("=" * 66)
    test_failure_is_never_reported_as_agreement()
    test_clean_consensus_is_allowed()
    test_unsupported_method_is_an_answer_not_a_failure()
    test_a_newer_round_wins_even_with_a_failure()
    test_skipped_counts_as_unresolved()
    test_baseline_comes_from_the_first_successful_round()

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
