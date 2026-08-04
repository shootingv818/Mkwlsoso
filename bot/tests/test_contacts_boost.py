"""Tests for Contact Boost (contacts_boost/).

Run: python -m bot.tests.test_contacts_boost

No browser and no network: a fake driver answers importContacts / getContacts
the way the live account was measured to answer them. The parts under test are
the exact things that were wrong in the existing contacts job:

  * a number is NEVER probed twice for the same account. `expand_range()` always
    restarts at index 0, so a second run submits the identical numbers and
    cannot add anybody. The cursor here must move.
  * the cursor moves after EVERY batch, so a stop or a kill half way costs
    nothing (the send-ledger lesson: what is only in memory is lost).
  * the reported increase is MEASURED with getContacts, not taken from the
    server's `imported` tally -- which counts a number as imported even when it
    was already a contact, and so over-reports on any re-run.
  * a FLOOD answer is waited out and the run resumes, instead of aborting.
  * numbers are NOT consumed by a batch the server refused.
  * the phone format is probed once and then remembered.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_boost_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")
# Pacing is real seconds by default; zero it so the suite is fast AND
# deterministic. The flood wait is tested with an explicit fake clock instead.
os.environ["CONTACT_CREATE_DELAY"] = "0"


def _stub_playwright_module() -> None:
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


_stub_playwright_module()

from bot import contacts_store  # noqa: E402
from contacts_boost import cards as boost_cards  # noqa: E402
from contacts_boost import engine, numbers  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def fresh(account: str = "acct") -> str:
    """A clean account name so each test starts with no memory.

    The SHARED position is wiped too, otherwise one test's blocks would push the
    next test's starting point and the assertions would drift.
    """
    numbers.forget(account)
    contacts_store.forget(account)
    try:
        p = numbers.shared_path()
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    return account


# --------------------------------------------------------------------------
# A fake driver that behaves like the measured Eitaa build
# --------------------------------------------------------------------------

class FakeDriver:
    """Answers the two bridge calls the engine uses.

    `real_numbers` is the set of phone numbers that "exist" on Eitaa. Everything
    else is silently unmatched, which is exactly how the server behaves.
    """

    def __init__(self, real_numbers=(), *, good_format: str = "98",
                 flood_on_call: set | None = None, flood_wait: int = 5,
                 fail_on_call: set | None = None,
                 contacts_bridge: bool = True,
                 list_bridge: bool = True,
                 already_contacts: int = 0):
        self.real = {str(n).lstrip("+") for n in real_numbers}
        self.good_format = good_format
        self.flood_on_call = set(flood_on_call or ())
        self.flood_wait = flood_wait
        self.fail_on_call = set(fail_on_call or ())
        self.contacts_bridge = contacts_bridge
        self.list_bridge = list_bridge
        # Contacts the account starts with (they count towards getContacts but
        # are NOT in `real`, so they can never be matched again).
        self.contacts: list[dict] = [
            {"peer_id": f"pre{i}", "access_hash": f"h{i}", "title": f"Old {i}"}
            for i in range(already_contacts)]
        self.calls = 0
        self.submitted: list[list[str]] = []
        self.formats_tried: list[str] = []
        self.list_calls = 0

    async def ensure_contacts_bridge(self) -> bool:
        return self.contacts_bridge

    async def ensure_contacts_list_bridge(self) -> bool:
        return self.list_bridge

    async def bridge_import_contacts(self, entries, plus_prefix=False):
        self.calls += 1
        fmt = "+98" if plus_prefix else "98"
        self.formats_tried.append(fmt)
        if self.calls in self.flood_on_call:
            return {"ok": False, "limit": True,
                    "code": f"FLOOD_WAIT_{self.flood_wait}",
                    "wait": self.flood_wait}
        if self.calls in self.fail_on_call:
            return {"ok": False, "code": "INTERNAL_ERROR"}
        submitted = [str(e["phone"]).lstrip("+") for e in entries]
        self.submitted.append(submitted)
        if fmt != self.good_format:
            # THE TRAP: the wrong format matches nobody, with no error at all.
            return {"ok": True, "batch": len(entries), "imported_count": 0,
                    "users_count": 0, "retry_count": 0, "added": []}
        added = []
        for phone in submitted:
            if phone in self.real:
                added.append({"user_id": phone, "access_hash": "ah" + phone,
                              "phone": phone, "first": phone})
                self.contacts.append({"peer_id": phone, "access_hash": "ah" + phone,
                                      "title": phone})
        return {"ok": True, "batch": len(entries), "imported_count": len(added),
                "users_count": len(added), "retry_count": 0, "added": added}

    async def bridge_contacts_list(self):
        self.list_calls += 1
        if not self.list_bridge:
            return {"ok": False}
        return {"ok": True, "count": len(self.contacts),
                "contacts": list(self.contacts), "skipped": 0}


# --------------------------------------------------------------------------
# numbers.py -- the cursor
# --------------------------------------------------------------------------

def test_prefix_validation():
    print("\nthe prefix is validated before anything is probed")
    good, err = numbers.normalize_prefix("0916")
    check("a plain 09 prefix is accepted", good == "0916" and err is None, good)
    check("98... is normalised", numbers.normalize_prefix("98916")[0] == "0916")
    check("9... is normalised", numbers.normalize_prefix("916")[0] == "0916")
    check("a landline prefix is refused",
          numbers.normalize_prefix("021")[1] is not None)
    check("a whole number is refused",
          numbers.normalize_prefix("09164600000")[1] is not None)
    check("too short is refused", numbers.normalize_prefix("09")[1] is not None)
    check("0916 covers 10 million numbers",
          numbers.capacity("0916") == 10_000_000,
          f"{numbers.capacity('0916'):,}")
    check("091646 covers 100 thousand",
          numbers.capacity("091646") == 100_000)


def wipe_shared() -> None:
    p = numbers.shared_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def test_each_account_gets_a_DIFFERENT_block():
    print("\nevery account gets its OWN block -- no two collect the same people")
    wipe_shared()
    for a in ("acc1", "acc2", "acc3"):
        numbers.forget(a)
    got = {}
    for a in ("acc1", "acc2", "acc3"):
        entries, start, err = numbers.next_numbers(a, "0913151", 400)
        check(f"{a} got a block", len(entries) == 400 and err is None, str(err))
        got[a] = (start, {e["phone"] for e in entries})
    check("account 1 starts at 0", got["acc1"][0] == 0, str(got["acc1"][0]))
    check("account 2 starts at 400", got["acc2"][0] == 400, str(got["acc2"][0]))
    check("account 3 starts at 800", got["acc3"][0] == 800, str(got["acc3"][0]))
    a1, a2, a3 = (got[a][1] for a in ("acc1", "acc2", "acc3"))
    check("1 and 2 share NO number", not (a1 & a2), str(len(a1 & a2)))
    check("1 and 3 share NO number", not (a1 & a3), str(len(a1 & a3)))
    check("2 and 3 share NO number", not (a2 & a3), str(len(a2 & a3)))
    check("the shared position is at 1200", numbers.shared_cursor("0913151") == 1200,
          str(numbers.shared_cursor("0913151")))
    who = numbers.blocks("0913151")
    check("who got what is recorded", len(who) == 3, str(who))
    check("the ranges are contiguous",
          [(b["from"], b["to"]) for b in who] == [(0, 400), (400, 800), (800, 1200)],
          str([(b["from"], b["to"]) for b in who]))


def test_two_boosts_at_once_cannot_get_the_same_block():
    print("\nmulti-parallel: the block is claimed UP FRONT, not batch by batch")
    wipe_shared()
    numbers.forget("p1")
    numbers.forget("p2")
    # Both ask before either has submitted anything -- the race that would hand
    # out the same numbers if the position only moved as batches completed.
    e1, s1, _ = numbers.next_numbers("p1", "0913151", 400)
    e2, s2, _ = numbers.next_numbers("p2", "0913151", 400)
    check("the second one is pushed past the first", s2 == s1 + 400,
          f"{s1} then {s2}")
    check("and their numbers do not overlap",
          not ({e["phone"] for e in e1} & {e["phone"] for e in e2}))


def test_an_unused_tail_goes_back():
    print("\na run that stops early hands its unused numbers back")
    wipe_shared()
    numbers.forget("tail1")
    _, start, _ = numbers.next_numbers("tail1", "0913151", 400)
    check("400 were claimed", numbers.shared_cursor("0913151") == 400)
    numbers.release_unused("tail1", "0913151", start, used=100, reserved=400)
    check("only the 100 used stay claimed",
          numbers.shared_cursor("0913151") == 100,
          str(numbers.shared_cursor("0913151")))
    # ...but not if somebody reserved on top, or two accounts would collide.
    _, s2, _ = numbers.next_numbers("tail2", "0913151", 50)
    numbers.release_unused("tail1", "0913151", 0, used=10, reserved=100)
    check("a tail under somebody else's block is NOT reclaimed",
          numbers.shared_cursor("0913151") == 150,
          str(numbers.shared_cursor("0913151")))


def test_migration_from_the_old_per_account_position():
    print("\nswitching to a shared range does not re-hand numbers an account had")
    wipe_shared()
    numbers.forget("old1")
    numbers.forget("old2")
    # Simulate the account that was already boosted before this change: it holds
    # 0..400 under 0913151 in its own file, and the shared file does not exist.
    numbers.advance("old1", "0913151", probed=0, hits=0)
    data = numbers.load("old1")
    data["prefixes"]["0913151"]["cursor"] = 400
    numbers._write("old1", data)
    wipe_shared()
    check("the shared position adopts it", numbers.shared_cursor("0913151") == 400,
          str(numbers.shared_cursor("0913151")))
    entries, start, _ = numbers.next_numbers("old2", "0913151", 400)
    check("the next account starts AFTER it", start == 400, str(start))
    check("so it cannot get the first account's people",
          "+989131510000" not in {e["phone"] for e in entries})


def test_shared_can_be_turned_off():
    print("\nMKWL_BOOST_SHARED_RANGE=0 restores the old per-account behaviour")
    wipe_shared()
    numbers.forget("off1")
    numbers.forget("off2")
    import config as config_mod
    original = config_mod.config.BOOST_SHARED_RANGE
    config_mod.config.BOOST_SHARED_RANGE = False
    try:
        _, s1, _ = numbers.next_numbers("off1", "0913151", 400)
        _, s2, _ = numbers.next_numbers("off2", "0913151", 400)
        check("both accounts start at 0", s1 == 0 and s2 == 0, f"{s1} / {s2}")
        check("the shared file was not used",
              not numbers.shared_path().is_file())
    finally:
        config_mod.config.BOOST_SHARED_RANGE = original


def test_no_number_is_ever_probed_twice():
    print("\nthe SAME number is never probed twice for one account")
    acct = fresh("dup")
    first, start1, err = numbers.next_numbers(acct, "091646", 10)
    check("the first block starts at index 0", start1 == 0 and err is None)
    numbers.advance(acct, "091646", probed=len(first), hits=3)
    second, start2, _ = numbers.next_numbers(acct, "091646", 10)
    check("the second block starts after the first", start2 == 10, str(start2))
    overlap = {e["phone"] for e in first} & {e["phone"] for e in second}
    check("the two blocks share no number", not overlap, str(overlap))
    # This is the exact bug in the existing job, shown side by side.
    from bot.runner import expand_range
    old_a, _ = expand_range("091646", 10)
    old_b, _ = expand_range("091646", 10)
    check("expand_range() DOES repeat itself (the bug being fixed)",
          [e["phone"] for e in old_a] == [e["phone"] for e in old_b])


def test_cursor_survives_and_reports():
    print("\nthe cursor is on disk, so a restart continues instead of repeating")
    acct = fresh("persist")
    numbers.next_numbers(acct, "091646", 400)      # claims 0..400
    numbers.advance(acct, "091646", probed=400, hits=87)
    check("the position is remembered", numbers.cursor(acct, "091646") == 400,
          str(numbers.cursor(acct, "091646")))
    st = numbers.stats(acct, "091646")
    check("tried is counted", st["tried"] == 400)
    check("hits are counted", st["hits"] == 87)
    check("what is left is reported", st["left"] == 100_000 - 400, f"{st['left']:,}")
    check("the next number is nameable",
          numbers.label("091646", 400) == "09164600400",
          numbers.label("091646", 400))
    check("the format is remembered once learned",
          (numbers.remember_format(acct, "+98"),
           numbers.phone_format(acct))[1] == "+98")


def test_exhausted_prefix_is_refused():
    print("\na prefix that has been fully probed says so instead of looping")
    acct = fresh("full")
    numbers.reserve(acct, "09164", numbers.capacity("09164"))
    entries, _, err = numbers.next_numbers(acct, "09164", 10)
    check("no entries are handed back", entries == [])
    check("the reason is explained", err is not None and "probed" in err, str(err))


def test_partial_block_at_the_end():
    print("\nthe last block is short rather than running past the prefix")
    acct = fresh("tail")
    cap = numbers.capacity("09164")
    numbers.reserve(acct, "09164", cap - 3)
    entries, _, err = numbers.next_numbers(acct, "09164", 400)
    check("only what is left is returned", len(entries) == 3, str(len(entries)))
    check("no error for a short tail", err is None)


# --------------------------------------------------------------------------
# engine.py
# --------------------------------------------------------------------------

def test_boost_measures_the_increase():
    print("\nthe increase is MEASURED before/after, not taken from the server")
    acct = fresh("measure")
    real = ["98916460000" + str(i) for i in range(5)]      # 5 numbers exist
    d = FakeDriver(real, already_contacts=12)
    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=100, contacts_before=12))
    check("it ran", state["ok"], str(state.get("reason")))
    check("100 numbers were probed", state["probed"] == 100, str(state["probed"]))
    check("5 matched", state["matched"] == 5, str(state["matched"]))
    check("before is 12", state["contacts_before"] == 12)
    check("after is 17", state["contacts_after"] == 17, str(state["contacts_after"]))
    check("the increase is +5", state["increase"] == 5)
    check("getContacts was consulted for the real count", d.list_calls >= 1)
    check("the fresh list was cached so the new contacts are sendable",
          contacts_store.count(acct) == 17, str(contacts_store.count(acct)))


def test_matched_but_already_a_contact_is_not_counted_as_growth():
    print("\na number that was ALREADY a contact does not inflate the increase")
    print("  (this is the over-reporting in the existing job)")
    acct = fresh("already")
    real = ["98916460000" + str(i) for i in range(4)]
    d = FakeDriver(real, already_contacts=0)
    # First run: these 4 become contacts.
    s1 = run(engine.boost(d, acct, "989999999999", prefix="091646", probe=10,
                          contacts_before=0))
    check("the first run gains 4", s1["increase"] == 4, str(s1["increase"]))
    # Now re-probe the SAME numbers by rewinding the position, which is what the
    # old expand_range() did on every single run. Both the account's record AND
    # the shared position have to go: forget() deliberately leaves the shared one
    # alone so a re-added account cannot be handed somebody else's block.
    numbers.forget(acct)
    wipe_shared()
    before = len(d.contacts)
    s2 = run(engine.boost(d, acct, "989999999999", prefix="091646", probe=10,
                          contacts_before=before))
    check("the server still calls them imported", s2["matched"] == 4,
          str(s2["matched"]))
    check("but the measured increase is 0", s2["increase"] == 0,
          str(s2["increase"]))
    card = engine.summary_card(acct, "989999999999", s2)
    check("the card admits they were already contacts",
          "Already had : 4" in card)
    check("the card does not claim DONE with growth",
          "Increase : +0" in card)


def test_cursor_advances_per_batch_not_at_the_end():
    print("\nthe cursor is saved after EVERY batch, so a stop costs nothing")
    acct = fresh("perbatch")
    # Somebody in the first batch exists, so the phone format settles
    # immediately and every chunk is exactly one submission.
    d = FakeDriver(["98916460000" + str(i) for i in range(3)], already_contacts=0)

    def should_stop() -> bool:
        # Stop the moment two batches have gone out, i.e. mid-run.
        return len(d.submitted) >= 2

    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=400, contacts_before=0,
                             should_stop=should_stop))
    cur = numbers.cursor(acct, "091646")
    check("it stopped early", state["stopped"], str(state))
    check("only the submitted numbers were consumed",
          cur == len(d.submitted) * engine.BATCH, f"cursor={cur}")
    check("that is less than the whole block", cur < 400, f"cursor={cur}")
    # The next run must continue, not repeat.
    nxt, start, _ = numbers.next_numbers(acct, "091646", 10)
    check("the next run continues from there", start == cur, str(start))


def test_flood_is_waited_out_and_the_run_resumes():
    print("\na FLOOD answer is waited out instead of killing the run")
    acct = fresh("flood")
    real = ["98916460000" + str(i) for i in range(3)]
    d = FakeDriver(real, flood_on_call={2}, flood_wait=1)
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s, *a, **k):
        slept.append(s)
        return await real_sleep(0)

    asyncio.sleep = fake_sleep
    try:
        state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                                 probe=150, contacts_before=0))
    finally:
        asyncio.sleep = real_sleep
    check("it finished anyway", state["ok"], str(state.get("reason")))
    check("it slept the requested time", 1 in slept, str(slept))
    check("the wait is reported", state["waited"] == 1, str(state["waited"]))
    check("it is flagged as rate limited", state["rate_limited"])
    check("all 150 numbers were still probed", state["probed"] == 150,
          str(state["probed"]))


def test_a_refused_batch_does_not_burn_its_numbers():
    print("\nnumbers in a batch the server REFUSED are not marked as used")
    acct = fresh("refused")
    # A wait longer than the engine will sit through -> the run gives up.
    d = FakeDriver([], flood_on_call={1}, flood_wait=99999)
    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=400, contacts_before=0))
    check("nothing was counted as probed", state["probed"] == 0,
          str(state["probed"]))
    check("the cursor did not move", numbers.cursor(acct, "091646") == 0,
          str(numbers.cursor(acct, "091646")))
    check("the reason is on the card", bool(state.get("note")), str(state.get("note")))
    card = engine.summary_card(acct, "989999999999", state)
    check("the card says PARTIAL, not DONE", "PARTIAL" in card)


def test_phone_format_is_probed_once_then_remembered():
    print("\nthe 98 / +98 format is probed once, then never again")
    acct = fresh("fmt")
    real = ["+98916460000" + str(i) for i in range(3)]
    d = FakeDriver([n.lstrip("+") for n in real], good_format="+98")
    s1 = run(engine.boost(d, acct, "989999999999", prefix="091646", probe=50,
                          contacts_before=0))
    check("it found the working format", s1["phone_format"] == "+98",
          str(s1["phone_format"]))
    check("it tried the plain form first", d.formats_tried[0] == "98")
    check("it retried the SAME numbers in the other form",
          d.formats_tried[:2] == ["98", "+98"], str(d.formats_tried[:2]))
    check("the format is stored", numbers.phone_format(acct) == "+98")
    calls_before = len(d.formats_tried)
    run(engine.boost(d, acct, "989999999999", prefix="091646", probe=50,
                     contacts_before=len(d.contacts)))
    check("the second run does not probe the format again",
          d.formats_tried[calls_before:] == ["+98"],
          str(d.formats_tried[calls_before:]))


def test_an_empty_block_does_not_pin_the_wrong_format():
    print("\na block where NOBODY exists must not lock in a phone format")
    print("  (both forms answer 'imported: 0', so neither is proof)")
    acct = fresh("nopin")
    d = FakeDriver([], good_format="+98")      # nobody exists at all
    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=400, contacts_before=0))
    check("no format was remembered", numbers.phone_format(acct) is None,
          str(numbers.phone_format(acct)))
    check("and none is claimed on the card", state["phone_format"] is None)
    # ...and it must not double every call for the whole run either.
    check("the double-probe is budgeted, not endless",
          d.calls < 2 * (400 // engine.BATCH), f"calls={d.calls}")
    check("it says what it did", "98 form only" in (state.get("note") or ""),
          str(state.get("note")))
    # A later run, once a real number turns up, still learns the right format.
    d2 = FakeDriver(["98916460040" + str(i) for i in range(3)], good_format="+98")
    d2.real = {"+98916460040" + str(i) for i in range(3)}
    d2.real = {n.lstrip("+") for n in d2.real}
    s2 = run(engine.boost(d2, acct, "989999999999", prefix="091646",
                          probe=100, contacts_before=0))
    check("the next run still probes both formats", "98" in d2.formats_tried
          and "+98" in d2.formats_tried, str(d2.formats_tried[:2]))
    check("and now it learns the right one", numbers.phone_format(acct) == "+98",
          str(numbers.phone_format(acct)))


def test_probe_is_a_number_of_probes_not_a_target():
    print("\nprobe=400 means 400 NUMBERS, not 400 contacts")
    acct = fresh("nottarget")
    d = FakeDriver([])          # nobody exists at all
    state = run(engine.boost(d, acct, "091646" and "091646" or "", prefix="091646",
                             probe=400, contacts_before=0))
    check("it probed exactly 400", state["probed"] == 400, str(state["probed"]))
    check("it added nobody, and says so", state["matched"] == 0)
    check("it did NOT keep going looking for more",
          numbers.cursor(acct, "091646") == 400,
          str(numbers.cursor(acct, "091646")))
    card = engine.summary_card(acct, "989999999999", state)
    check("the card says NOBODY FOUND rather than DONE", "NOBODY FOUND" in card)
    check("the card explains the next run moves on", "NEXT block" in card
          or "next" in card.lower())


def test_peers_are_counted_once_not_carded_per_batch():
    print("\nno 'PEERS SAVED' card per batch -- one line on the summary instead")
    acct = fresh("peers")
    # Enough real numbers that several batches each match somebody.
    real = ["9891646" + str(i).zfill(5) for i in range(0, 200, 7)]
    d = FakeDriver(real, already_contacts=0)
    cards_posted: list[str] = []
    saved_calls: list[int] = []

    async def rep(t):
        cards_posted.append(t)

    async def save_peers(rows):
        saved_calls.append(len(rows))
        return len(rows)          # pretend every one was new

    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=200, contacts_before=0, report=rep,
                             save_peers=save_peers))
    check("several batches did import somebody", len(saved_calls) >= 3,
          str(saved_calls))
    check("but no PEERS SAVED card was posted",
          not any("PEERS SAVED" in c for c in cards_posted),
          str([c.splitlines()[0] for c in cards_posted]))
    check("the total was accumulated instead",
          state["peers_new"] == sum(saved_calls), str(state["peers_new"]))
    state["peers_total"] = 284
    card = engine.summary_card(acct, "989999999999", state)
    check("and it appears once on the summary card",
          f"Fast-send ready : +{state['peers_new']:,}" in card, card)
    check("with the running total", "(284 total)" in card)
    check("exactly one such line", card.count("Fast-send ready") == 1)


def test_no_prefix_skips_politely():
    print("\nno prefix set = a skip card, not a crash")
    acct = fresh("noprefix")
    d = FakeDriver([])
    said: list[str] = []

    async def rep(t):
        said.append(t)

    state = run(engine.boost(d, acct, "989999999999", prefix="", probe=400,
                             report=rep))
    check("it did not run", not state["ok"])
    check("a reason came back", bool(state["reason"]))
    check("a skip card was posted", any("SKIPPED" in s for s in said))
    check("no numbers were touched", d.calls == 0)


def test_missing_bridge_skips_instead_of_clicking():
    print("\nno bridge = skip (the per-number UI path is not run unattended)")
    acct = fresh("nobridge")
    d = FakeDriver([], contacts_bridge=False)
    said: list[str] = []

    async def rep(t):
        said.append(t)

    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=400, report=rep))
    check("it did not run", not state["ok"])
    check("it said why", any("SKIPPED" in s for s in said))
    check("the cursor did not move", numbers.cursor(acct, "091646") == 0)


def test_unreadable_count_falls_back_honestly():
    print("\nif getContacts cannot be read, the card says the number is the "
          "server's own")
    acct = fresh("nolist")
    real = ["98916460000" + str(i) for i in range(2)]
    d = FakeDriver(real, list_bridge=False)
    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=50, contacts_before=7))
    check("it still completed", state["ok"])
    check("it fell back to before+matched", state["contacts_after"] == 9,
          str(state["contacts_after"]))
    check("and it says the count is not measured",
          "could not be re-read" in (state.get("note") or ""),
          str(state.get("note")))


def test_live_card_shows_the_counts():
    print("\nthe live card shows the real count and the increase")
    text = boost_cards.progress(
        account="989213725238", phone="989213725238", prefix="091646",
        status="RUNNING", step="IMPORTING", probe_total=400, probed=200,
        matched=51, contacts_before=12, contacts_now=63,
        first_number="09164600400", last_number="09164600799",
        phone_format="98", waited=0, elapsed=95.0)
    check("the header is the owner's shape", text.startswith("| \u2699 - #boost"))
    check("the phone line is there", "--| Phone - 989213725238" in text)
    check("the prefix is shown", "Prefix : 091646" in text)
    check("the range is shown", "09164600400 \u2192 09164600799" in text)
    check("progress is 200 of 400", "200 / 400" in text)
    check("the hit rate is shown", "Hit rate : 26%" in text, text)
    check("a bar is drawn", "\u2588" in text and "\u2591" in text)
    check("the worker footer is there", "Worker : #W" in text)


def test_final_card_reports_both_numbers():
    print("\nthe result card reports the real count AND the increase")
    text = boost_cards.finished(
        account="989213725238", phone="989213725238", prefix="091646",
        probe_total=400, probed=400, matched=87, contacts_before=12,
        contacts_after=99, elapsed=161.0, first_number="09164600400",
        next_number="09164600800", phone_format="98", waited=0,
        left_under_prefix=99_200, lifetime_tried=400, lifetime_hits=87)
    for want in ("Numbers probed : 400", "Matched on Eitaa : 87",
                 "Contacts before : 12", "Contacts after : 99",
                 "Increase : +87", "Hit rate : 22%",
                 "Next run starts at : 09164600800"):
        check(f"card shows {want!r}", want in text, text if want not in text else "")


def main() -> int:
    print("=" * 68)
    print("  CONTACT BOOST TESTS")
    print("=" * 68)
    try:
        test_prefix_validation()
        test_each_account_gets_a_DIFFERENT_block()
        test_two_boosts_at_once_cannot_get_the_same_block()
        test_an_unused_tail_goes_back()
        test_migration_from_the_old_per_account_position()
        test_shared_can_be_turned_off()
        test_no_number_is_ever_probed_twice()
        test_cursor_survives_and_reports()
        test_exhausted_prefix_is_refused()
        test_partial_block_at_the_end()
        test_boost_measures_the_increase()
        test_matched_but_already_a_contact_is_not_counted_as_growth()
        test_cursor_advances_per_batch_not_at_the_end()
        test_flood_is_waited_out_and_the_run_resumes()
        test_a_refused_batch_does_not_burn_its_numbers()
        test_phone_format_is_probed_once_then_remembered()
        test_an_empty_block_does_not_pin_the_wrong_format()
        test_probe_is_a_number_of_probes_not_a_target()
        test_peers_are_counted_once_not_carded_per_batch()
        test_no_prefix_skips_politely()
        test_missing_bridge_skips_instead_of_clicking()
        test_unreadable_count_falls_back_honestly()
        test_live_card_shows_the_counts()
        test_final_card_reports_both_numbers()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL CONTACT BOOST TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
