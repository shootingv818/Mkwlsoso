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
    contacts_store.forget(account)
    wipe_all()
    return account


# --------------------------------------------------------------------------
# A fake driver that behaves like the measured Eitaa build
# --------------------------------------------------------------------------

class FakeDriver:
    """Answers the two bridge calls the engine uses.

    Two ways to say which numbers "exist" on Eitaa:

      * `real_numbers` -- an explicit set. Use it when the test cares about
        SPECIFIC numbers.
      * `match_every` -- every Nth number submitted across the whole run
        matches. Use it for engine tests: numbers are now drawn at RANDOM, so a
        test cannot know which ones will come out, and matching by position
        keeps the expected count exact anyway.

    Everything else is silently unmatched, with no error, which is exactly how
    the server behaves when a number is not registered.
    """

    def __init__(self, real_numbers=(), *, good_format: str = "98",
                 match_every: int | None = None,
                 flood_on_call: set | None = None, flood_wait: int = 5,
                 fail_on_call: set | None = None,
                 contacts_bridge: bool = True,
                 list_bridge: bool = True,
                 already_contacts: int = 0):
        self.real = {str(n).lstrip("+") for n in real_numbers}
        self.match_every = match_every
        self.good_format = good_format
        self.flood_on_call = set(flood_on_call or ())
        self.flood_wait = flood_wait
        self.fail_on_call = set(fail_on_call or ())
        self.contacts_bridge = contacts_bridge
        self.list_bridge = list_bridge
        # Contacts the account starts with. Keyed by peer_id, like Eitaa: adding
        # a number that is ALREADY a contact does not grow the list.
        self.contact_map: dict = {
            f"pre{i}": {"peer_id": f"pre{i}", "access_hash": f"h{i}",
                        "title": f"Old {i}"}
            for i in range(already_contacts)}
        self.calls = 0
        self.seen = 0            # numbers submitted so far, across the run
        self.submitted: list[list[str]] = []
        self.formats_tried: list[str] = []
        self.list_calls = 0

    @property
    def contacts(self) -> list:
        return list(self.contact_map.values())

    def _exists(self, phone: str, position: int) -> bool:
        if self.match_every:
            return position % self.match_every == 0
        return phone in self.real

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
            position, self.seen = self.seen, self.seen + 1
            if not self._exists(phone, position):
                continue
            added.append({"user_id": phone, "access_hash": "ah" + phone,
                          "phone": phone, "first": phone})
            # Eitaa answers "imported" even when the user is ALREADY a contact,
            # so the row goes into `added` either way -- but the contact list
            # does not grow twice. That gap is what the card has to be honest
            # about.
            self.contact_map.setdefault(phone, {
                "peer_id": phone, "access_hash": "ah" + phone, "title": phone})
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
    """Remove only the SHARED memory (per-account files survive)."""
    p = numbers.shared_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def wipe_all() -> None:
    """A real clean slate.

    The migration scan reads EVERY boost_*.json in DATA_DIR to fold old
    sequential positions in, so a leftover file from one test would seed the
    next one's memory. Production never wipes the shared file, so this is a test
    concern only.
    """
    from config import config as _cfg
    for path in _cfg.DATA_DIR.glob("boost_*.json"):
        try:
            path.unlink()
        except OSError:
            pass


def test_several_prefixes_can_be_set():
    print("\nseveral prefixes can be given at once, however they are typed")
    good, bad = numbers.parse_prefixes("0913151, 0913152 0913153")
    check("all three parsed", good == ["0913151", "0913152", "0913153"], str(good))
    check("nothing rejected", not bad, str(bad))
    good, bad = numbers.parse_prefixes("0913151\n0913151\n09131 52")
    check("duplicates dropped", good.count("0913151") == 1, str(good))
    good, bad = numbers.parse_prefixes("0913151, 021445, hello")
    check("the good one survives", "0913151" in good, str(good))
    check("the bad ones are reported with a reason", len(bad) == 2, str(bad))
    check("and the reason is readable", "09" in bad[0][1], str(bad[0]))


def test_one_prefix_is_picked_at_random_per_run():
    print("\nONE prefix is picked at RANDOM each run, not used up in turn")
    wipe_shared()
    acct = fresh("pick")
    raw = "0913151, 0913152, 0913153"
    seen = {}
    for _ in range(60):
        p, err, info = numbers.choose_prefix(acct, raw)
        check_once = err is None and p
        if not check_once:
            check("a prefix was chosen", False, str(err))
            return
        seen[p] = seen.get(p, 0) + 1
        check_once = None
    check("all three get chosen over 60 runs", len(seen) == 3, str(seen))
    check("none of them dominates", all(5 <= n <= 40 for n in seen.values()),
          str(seen))
    check("the pool size is reported", info["pool"] == 3, str(info))


def test_numbers_stay_unique_across_prefixes():
    print("\nnumbers never repeat, whichever prefix they came from")
    wipe_shared()
    acct = fresh("across")
    raw = "0913151, 0913152, 0913153"
    seen: set[str] = set()
    for _ in range(15):
        p, err, _ = numbers.choose_prefix(acct, raw)
        if err:
            break
        entries, idx, err = numbers.next_numbers(acct, p, 200)
        if err:
            continue
        phones = {e["phone"] for e in entries}
        overlap = seen & phones
        if overlap:
            check("no number was handed out twice", False, str(sorted(overlap)[:5]))
            return
        seen.update(phones)
    check("3,000 numbers, all distinct", len(seen) == 3000, str(len(seen)))
    check("and they span more than one prefix",
          len({p[:10] for p in seen}) > 1, str(len({p[:10] for p in seen})))


def test_an_exhausted_prefix_drops_out_of_the_pool():
    print("\na prefix with nothing left stops being offered")
    wipe_all()
    acct = "drained"
    raw = "091315123, 0913152"          # the first holds only 100 numbers
    numbers.draw(acct, "091315123", 100)
    check("the small one is full",
          numbers.stats(acct, "091315123")["left"] == 0)
    for _ in range(20):
        p, err, info = numbers.choose_prefix(acct, raw)
        check("something was still chosen", err is None and p, str(err))
        if p != "0913152":
            check("the full prefix was not offered", False, p)
            return
    check("only the one with numbers left is offered", True)
    check("and the card can say which was skipped",
          info["full"] == ["091315123"], str(info["full"]))


def test_a_prefix_that_finds_nobody_is_retired():
    print("\na prefix that has been sampled and found NOBODY stops wasting runs")
    wipe_shared()
    acct = fresh("empty-block")
    raw = "0913151, 0913152"
    # 0913152 sampled 400 times, nobody found. 0913151 sampled 400, 284 found.
    numbers.draw(acct, "0913152", 400)
    numbers.advance(acct, "0913152", probed=400, hits=0)
    numbers.draw(acct, "0913151", 400)
    numbers.advance(acct, "0913151", probed=400, hits=284)
    for _ in range(20):
        p, err, info = numbers.choose_prefix(acct, raw)
        if p != "0913151":
            check("the empty prefix was skipped", False, p)
            return
    check("only the productive prefix is offered", True)
    check("the skip is reported so it is visible",
          info["dead"] == ["0913152"], str(info["dead"]))
    # ...and one hit is enough to bring it back. A 0% sample is evidence, not proof.
    numbers.advance(acct, "0913152", probed=0, hits=20)
    _, _, info2 = numbers.choose_prefix(acct, raw)
    check("one hit brings it back", not info2["dead"], str(info2["dead"]))


def test_retiring_never_leaves_nothing_to_do():
    print("\nif EVERY prefix looks empty it still runs, rather than refusing")
    print("  (a 0% sample is evidence, not proof)")
    wipe_shared()
    acct = fresh("all-empty")
    raw = "0913151, 0913152"
    for p in ("0913151", "0913152"):
        numbers.draw(acct, p, 400)
        numbers.advance(acct, p, probed=400, hits=0)
    p, err, info = numbers.choose_prefix(acct, raw)
    check("it still chose one", err is None and p in ("0913151", "0913152"),
          f"{p!r} err={err}")
    check("and both were flagged", len(info["dead"]) == 2, str(info["dead"]))


def test_the_migrated_prefix_is_not_mistaken_for_empty():
    print("\nthe prefix boosted BEFORE the shared memory existed keeps its hits")
    print("  (otherwise it would read '400 used, 0 found' and be retired)")
    wipe_all()
    # Exactly the shape the live account was left in: its own file holds the old
    # cursor AND the hits, and the shared file does not exist yet.
    numbers.advance("legacy", "0913151", probed=400, hits=284)
    data = numbers.load("legacy")
    data["prefixes"]["0913151"]["cursor"] = 400
    numbers._write("legacy", data)
    wipe_shared()
    st = numbers.stats("legacy", "0913151")
    check("the 400 numbers carried over", st["used"] == 400, str(st["used"]))
    check("and so did the 284 hits", st["hits_all"] == 284, str(st["hits_all"]))
    check("so it is NOT retired", not st["dead"] if "dead" in st else True)
    _, _, info = numbers.choose_prefix("legacy", "0913151")
    check("it stays in the pool", not info["dead"], str(info["dead"]))


def test_no_prefix_at_all_is_explained():
    print("\nan empty or unusable prefix setting says what to do")
    p, err, info = numbers.choose_prefix("nobody", "")
    check("nothing was chosen", not p)
    check("and it says why", err is not None and "prefix" in err, str(err))
    p, err, info = numbers.choose_prefix("nobody", "021445, 12")
    check("garbage is refused too", not p and err, str(err))
    check("with the reason from the first bad one", info["bad"], str(info["bad"]))


def test_the_pool_view_reports_each_prefix():
    print("\nthe Settings pool view reports every prefix separately")
    wipe_shared()
    acct = fresh("poolview")
    raw = "0913151, 0913152, 0913153"
    numbers.draw(acct, "0913151", 400)
    numbers.advance(acct, "0913151", probed=400, hits=284)
    rows = numbers.pool(acct, raw)
    check("one row per prefix", len(rows) == 3, str(len(rows)))
    check("the order given is kept",
          [r["prefix"] for r in rows] == ["0913151", "0913152", "0913153"],
          str([r["prefix"] for r in rows]))
    first = rows[0]
    check("the sampled one shows its hits", first["hits_all"] == 284,
          str(first["hits_all"]))
    check("and what is left", first["left"] == 9600, str(first["left"]))
    check("the untouched ones show full capacity",
          rows[1]["left"] == 10000 and rows[1]["used"] == 0, str(rows[1]))


def test_numbers_are_scattered_not_sequential():
    print("\nnumbers are picked at RANDOM, not walked in order")
    print("  (a sequential import is an obvious machine fingerprint)")
    acct = fresh("scatter")
    entries, idx, err = numbers.next_numbers(acct, "0913151", 400)
    check("400 numbers came back", len(idx) == 400 and err is None, str(err))
    check("they are all different", len(set(idx)) == 400)
    check("they are NOT consecutive", sorted(idx) != list(range(400)),
          "lowest few: " + str(sorted(idx)[:5]))
    gaps = {b - a for a, b in zip(sorted(idx), sorted(idx)[1:])}
    check("the spacing varies (no arithmetic pattern)", len(gaps) > 5,
          f"{len(gaps)} distinct gaps")
    check("they cover the whole prefix, not one corner",
          max(idx) - min(idx) > 8000, f"span {min(idx)}..{max(idx)}")
    check("and the entries carry real +98 numbers",
          all(e["phone"].startswith("+98913151") and len(e["phone"]) == 13
              for e in entries), entries[0]["phone"])


def test_a_number_is_never_handed_out_twice():
    print("\nthe same number is never handed out twice, to anybody")
    acct = fresh("once")
    seen: set[int] = set()
    for _ in range(10):
        _, idx, err = numbers.next_numbers(acct, "0913151", 400)
        check("a draw succeeded", len(idx) == 400 and err is None, str(err))
        overlap = seen & set(idx)
        if overlap:
            check("no overlap with earlier draws", False, str(sorted(overlap)[:5]))
            return
        seen.update(idx)
    check("4,000 drawn, zero repeats", len(seen) == 4000, str(len(seen)))
    check("the used-set knows about all of them",
          numbers.used_count(acct, "0913151") == 4000,
          str(numbers.used_count(acct, "0913151")))


def test_each_account_gets_DIFFERENT_numbers():
    print("\nevery account gets its own numbers -- no shared contacts")
    wipe_all()
    got = {}
    for a in ("acc1", "acc2", "acc3"):
        _, idx, err = numbers.next_numbers(a, "0913151", 400)
        check(f"{a} drew 400", len(idx) == 400 and err is None, str(err))
        got[a] = set(idx)
    a1, a2, a3 = got["acc1"], got["acc2"], got["acc3"]
    check("1 and 2 share NO number", not (a1 & a2), str(len(a1 & a2)))
    check("1 and 3 share NO number", not (a1 & a3), str(len(a1 & a3)))
    check("2 and 3 share NO number", not (a2 & a3), str(len(a2 & a3)))
    check("1,200 numbers are used in total",
          numbers.used_count("acc1", "0913151") == 1200,
          str(numbers.used_count("acc1", "0913151")))
    check("who drew how many is recorded", len(numbers.draws("0913151")) == 3)


def test_two_boosts_at_once_cannot_get_the_same_number():
    print("\nmulti-parallel: numbers are claimed at DRAW time")
    wipe_all()
    # Both ask before either has submitted anything -- the race that would hand
    # out the same numbers if they were only marked as batches completed.
    _, i1, _ = numbers.next_numbers("p1", "0913151", 400)
    _, i2, _ = numbers.next_numbers("p2", "0913151", 400)
    check("neither got any of the other's numbers", not (set(i1) & set(i2)),
          str(len(set(i1) & set(i2))))


def test_unused_numbers_go_back():
    print("\na run that stops early hands its unused numbers back")
    wipe_all()
    acct = "give-back"
    _, idx, _ = numbers.next_numbers(acct, "0913151", 400)
    check("400 are marked used", numbers.used_count(acct, "0913151") == 400)
    freed = numbers.undraw(acct, "0913151", idx[100:])
    check("300 went back", freed == 300, str(freed))
    check("only the 100 used remain",
          numbers.used_count(acct, "0913151") == 100,
          str(numbers.used_count(acct, "0913151")))
    # ...and they can be drawn again, by anybody.
    _, again, _ = numbers.next_numbers("someone-else", "0913151", 300)
    check("the returned numbers are available again",
          bool(set(again) & set(idx[100:])), "none came back round")
    check("but never the 100 that WERE used",
          not (set(again) & set(idx[:100])))


def test_the_packed_used_set_stays_small():
    print("\nthe memory is packed as ranges, so it does not bloat")
    check("a solid run collapses to one entry",
          numbers._pack(set(range(400))) == ["0-399"],
          str(numbers._pack(set(range(400)))))
    check("packing round-trips exactly",
          numbers._unpack(numbers._pack({1, 2, 3, 9, 40, 41})) == {1, 2, 3, 9, 40, 41})
    wipe_all()
    acct = "packed"
    numbers.next_numbers(acct, "0913151", 400)
    raw = numbers.shared_path().read_text(encoding="utf-8")
    check("the file stays well under 20 KB", len(raw) < 20_000, f"{len(raw)} bytes")
    check("and it is still exact", numbers.used_count(acct, "0913151") == 400)


def test_a_nearly_full_prefix_still_works():
    print("\nwhen the prefix is nearly used up, the draw is exact, not a guess")
    print("  (rejection sampling alone would spin at high density)")
    wipe_all()
    acct = "dense"
    cap = numbers.capacity("091315123")        # 100 numbers
    check("that prefix holds 100", cap == 100, str(cap))
    got: set[int] = set()
    err = None
    for _ in range(20):
        _, idx, err = numbers.next_numbers(acct, "091315123", 30)
        if err:
            break
        got.update(idx)
    check("all 100 were handed out, none twice", got == set(range(100)),
          str(len(got)))
    check("then it says the prefix is finished", err is not None, str(err))
    check("and explains it clearly", "different prefix" in (err or ""), str(err))


def test_sequential_mode_still_available():
    print("\nMKWL_BOOST_ORDER=sequential restores the in-order walk")
    wipe_all()
    import config as config_mod
    original = config_mod.config.BOOST_ORDER
    config_mod.config.BOOST_ORDER = "sequential"
    try:
        _, idx, _ = numbers.next_numbers("seq", "0913151", 10)
        check("it walks from the start", idx == list(range(10)), str(idx))
        _, idx2, _ = numbers.next_numbers("seq", "0913151", 10)
        check("and continues, never repeating", idx2 == list(range(10, 20)),
              str(idx2))
    finally:
        config_mod.config.BOOST_ORDER = original


def test_migration_from_the_old_sequential_records():
    print("\nupgrading does not re-hand numbers an account already holds")
    wipe_all()
    # The account boosted before this change: its file holds the old `cursor`.
    numbers.advance("old1", "0913151", probed=0, hits=0)
    data = numbers.load("old1")
    data["prefixes"]["0913151"]["cursor"] = 400
    numbers._write("old1", data)
    wipe_shared()
    check("the shared memory adopts those 400",
          numbers.used_count("old2", "0913151") == 400,
          str(numbers.used_count("old2", "0913151")))
    _, idx, _ = numbers.next_numbers("old2", "0913151", 400)
    check("the next account gets none of them",
          not (set(idx) & set(range(400))),
          str(sorted(set(idx) & set(range(400)))[:5]))


def test_shared_memory_can_be_turned_off():
    print("\nMKWL_BOOST_SHARED_RANGE=0 gives each account its own memory")
    wipe_all()
    import config as config_mod
    original = config_mod.config.BOOST_SHARED_RANGE
    config_mod.config.BOOST_SHARED_RANGE = False
    try:
        _, i1, _ = numbers.next_numbers("off1", "0913151", 400)
        _, i2, _ = numbers.next_numbers("off2", "0913151", 400)
        check("each account tracks its own", len(i1) == 400 and len(i2) == 400)
        check("so they CAN overlap now (that is the trade-off)", True,
              f"{len(set(i1) & set(i2))} shared")
        check("the shared file was not used",
              not numbers.shared_path().is_file())
    finally:
        config_mod.config.BOOST_SHARED_RANGE = original


def test_boost_measures_the_increase():
    print("\nthe increase is MEASURED before/after, not taken from the server")
    acct = fresh("measure")
    # Every 20th number submitted exists -> exactly 5 of 100, whichever numbers
    # the random draw happens to produce.
    d = FakeDriver(match_every=20, already_contacts=12)
    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=100, contacts_before=12))
    check("it ran", state["ok"], str(state.get("reason")))
    # The count is jittered on purpose, so the check is "it probed everything it
    # asked for", not a hard-coded 100.
    check("it probed the whole draw", state["probed"] == state["probe_total"],
          f'{state["probed"]} of {state["probe_total"]}')
    check("the draw is about 100", 90 <= state["probe_total"] <= 110,
          str(state["probe_total"]))
    # Every 20th of however many were drawn.
    expect = -(-state["probe_total"] // 20)
    check(f"{expect} matched", state["matched"] == expect, str(state["matched"]))
    check("before is 12", state["contacts_before"] == 12)
    check("after is before + matched", state["contacts_after"] == 12 + expect,
          str(state["contacts_after"]))
    check("the increase is what was matched", state["increase"] == expect,
          str(state["increase"]))
    check("getContacts was consulted for the real count", d.list_calls >= 1)
    check("the fresh list was cached so the new contacts are sendable",
          contacts_store.count(acct) == 12 + expect,
          str(contacts_store.count(acct)))


def test_matched_but_already_a_contact_is_not_counted_as_growth():
    print("\na number that was ALREADY a contact does not inflate the increase")
    print("  (this is the over-reporting in the existing job)")
    acct = fresh("already")
    import config as config_mod
    original = config_mod.config.BOOST_ORDER
    original_jitter = engine.JITTER_PCT
    # Sequential AND un-jittered, so the second run submits the IDENTICAL numbers
    # once the memory is wiped -- which is exactly what the old expand_range()
    # did on EVERY run.
    config_mod.config.BOOST_ORDER = "sequential"
    engine.JITTER_PCT = 0
    try:
        d = FakeDriver(match_every=3, already_contacts=0)
        s1 = run(engine.boost(d, acct, "989999999999", prefix="091646", probe=10,
                              contacts_before=0))
        gained = s1["increase"]
        check("the first run gains contacts", gained > 0, str(gained))
        # Rewind the memory and run again over the identical numbers.
        numbers.forget(acct)
        wipe_shared()
        d.seen = 0
        before = len(d.contacts)
        s2 = run(engine.boost(d, acct, "989999999999", prefix="091646", probe=10,
                              contacts_before=before))
    finally:
        config_mod.config.BOOST_ORDER = original
        engine.JITTER_PCT = original_jitter
    check("the server still calls them imported", s2["matched"] == gained,
          str(s2["matched"]))
    check("but the measured increase is 0", s2["increase"] == 0,
          str(s2["increase"]))
    card = engine.summary_card(acct, "989999999999", s2)
    check("the card admits they were already contacts",
          f"Already had : {gained}" in card, card)
    check("the card does not claim DONE with growth",
          "Increase : +0" in card)


def test_cursor_advances_per_batch_not_at_the_end():
    print("\nthe cursor is saved after EVERY batch, so a stop costs nothing")
    acct = fresh("perbatch")
    # Somebody in the first batch exists, so the phone format settles
    # immediately and every chunk is exactly one submission.
    d = FakeDriver(match_every=10, already_contacts=0)

    def should_stop() -> bool:
        # Stop the moment two batches have gone out, i.e. mid-run.
        return len(d.submitted) >= 2

    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=400, contacts_before=0,
                             should_stop=should_stop))
    used = numbers.used_count(acct, "091646")
    check("it stopped early", state["stopped"], str(state.get("stopped")))
    check("only the submitted numbers stayed used",
          used == len(d.submitted) * engine.BATCH, f"used={used}")
    check("the rest went back", used < state["probe_total"], f"used={used}")
    check("the returned count is reported", state.get("returned", 0) > 0,
          str(state.get("returned")))


def test_flood_is_waited_out_and_the_run_resumes():
    print("\na FLOOD answer is waited out instead of killing the run")
    acct = fresh("flood")
    d = FakeDriver(match_every=10, flood_on_call={2}, flood_wait=1)
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
    check("every number was still probed", state["probed"] == state["probe_total"],
          f'{state["probed"]} of {state["probe_total"]}')


def test_a_refused_batch_does_not_burn_its_numbers():
    print("\nnumbers in a batch the server REFUSED are not marked as used")
    acct = fresh("refused")
    # A wait longer than the engine will sit through -> the run gives up.
    d = FakeDriver([], flood_on_call={1}, flood_wait=99999)
    state = run(engine.boost(d, acct, "989999999999", prefix="091646",
                             probe=400, contacts_before=0))
    check("nothing was counted as probed", state["probed"] == 0,
          str(state["probed"]))
    check("no numbers stayed used", numbers.used_count(acct, "091646") == 0,
          str(numbers.used_count(acct, "091646")))
    check("the reason is on the card", bool(state.get("note")), str(state.get("note")))
    card = engine.summary_card(acct, "989999999999", state)
    check("the card says PARTIAL, not DONE", "PARTIAL" in card)


def test_phone_format_is_probed_once_then_remembered():
    print("\nthe 98 / +98 format is probed once, then never again")
    acct = fresh("fmt")
    d = FakeDriver(match_every=10, good_format="+98")
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
    after = d.formats_tried[calls_before:]
    check("the second run does not probe the format again",
          after and all(f == "+98" for f in after), str(after))


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
    d2 = FakeDriver(match_every=10, good_format="+98")
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
    check("it probed the whole draw and stopped", 
          state["probed"] == state["probe_total"], str(state["probed"]))
    check("the draw is about 400 (jittered on purpose)",
          360 <= state["probe_total"] <= 440, str(state["probe_total"]))
    check("it added nobody, and says so", state["matched"] == 0)
    check("it did NOT keep going looking for more",
          numbers.used_count(acct, "091646") == state["probe_total"],
          str(numbers.used_count(acct, "091646")))
    card = engine.summary_card(acct, "989999999999", state)
    check("the card says NOBODY FOUND rather than DONE", "NOBODY FOUND" in card)
    check("the card explains the next run picks different numbers",
          "different set" in card, card)


def test_peers_are_counted_once_not_carded_per_batch():
    print("\nno 'PEERS SAVED' card per batch -- one line on the summary instead")
    acct = fresh("peers")
    # Every 7th number matches, so several batches each import somebody.
    d = FakeDriver(match_every=7, already_contacts=0)
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
    check("no numbers were used up", numbers.used_count(acct, "091646") == 0)


def test_unreadable_count_falls_back_honestly():
    print("\nif getContacts cannot be read, the card says the number is the "
          "server's own")
    acct = fresh("nolist")
    d = FakeDriver(match_every=25, list_bridge=False)   # 2 of 50
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
        span=("09164600400", "09164609799"),
        phone_format="98", waited=0, elapsed=95.0)
    check("the header is the owner's shape", text.startswith("| \u2699 - #boost"))
    check("the phone line is there", "--| Phone - 989213725238" in text)
    check("the prefix is shown", "Prefix : 091646" in text)
    check("the span is shown", "09164600400" in text and "09164609799" in text)
    check("progress is 200 of 400", "200 / 400" in text)
    check("the hit rate is shown", "Hit rate : 26%" in text, text)
    check("a bar is drawn", "\u2588" in text and "\u2591" in text)
    check("the worker footer is there", "Worker : #W" in text)


def test_final_card_reports_both_numbers():
    print("\nthe result card reports the real count AND the increase")
    text = boost_cards.finished(
        account="989213725238", phone="989213725238", prefix="091646",
        probe_total=400, probed=400, matched=87, contacts_before=12,
        contacts_after=99, elapsed=161.0,
        span=("09164600400", "09164609799"), phone_format="98", waited=0,
        capacity=100_000, used_under_prefix=400, left_under_prefix=99_600,
        lifetime_tried=400, lifetime_hits=87)
    for want in ("Numbers probed : 400", "Matched on Eitaa : 87",
                 "Contacts before : 12", "Contacts after : 99",
                 "Increase : +87", "Hit rate : 22%",
                 "Picked : at random between 09164600400 and 09164609799"):
        check(f"card shows {want!r}", want in text, text if want not in text else "")


def main() -> int:
    print("=" * 68)
    print("  CONTACT BOOST TESTS")
    print("=" * 68)
    try:
        test_prefix_validation()
        test_several_prefixes_can_be_set()
        test_one_prefix_is_picked_at_random_per_run()
        test_numbers_stay_unique_across_prefixes()
        test_an_exhausted_prefix_drops_out_of_the_pool()
        test_a_prefix_that_finds_nobody_is_retired()
        test_retiring_never_leaves_nothing_to_do()
        test_the_migrated_prefix_is_not_mistaken_for_empty()
        test_no_prefix_at_all_is_explained()
        test_the_pool_view_reports_each_prefix()
        test_numbers_are_scattered_not_sequential()
        test_a_number_is_never_handed_out_twice()
        test_each_account_gets_DIFFERENT_numbers()
        test_two_boosts_at_once_cannot_get_the_same_number()
        test_unused_numbers_go_back()
        test_the_packed_used_set_stays_small()
        test_a_nearly_full_prefix_still_works()
        test_sequential_mode_still_available()
        test_migration_from_the_old_sequential_records()
        test_shared_memory_can_be_turned_off()
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
