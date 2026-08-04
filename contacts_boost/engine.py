"""The Contact Boost engine.

One run = probe a fixed number of unused numbers under the saved prefix, keep
whoever exists, report the real contact count before and after. It does NOT
chase a target: whatever the block happens to yield is the result, which is what
keeps the number of `importContacts` calls (and so the PEER_FLOOD risk on a
brand-new account) bounded and predictable.

Read-only towards other people: importing a contact does not message anybody.

Deliberate differences from bot.runner._contacts_job:

  * numbers come from contacts_boost.numbers (a per-account cursor), so a number
    is never probed twice for the same account;
  * the phone format is probed once per account and then remembered, instead of
    burning a double submission of the first batch on every single run;
  * a FLOOD/limit answer is waited out and the run resumes, instead of aborting
    the whole job on the first one;
  * the cursor is persisted after EVERY batch, so a stop or a kill costs nothing;
  * the reported increase is measured with contacts.getContacts, not taken from
    the server's `imported` tally.
"""

from __future__ import annotations

import asyncio
import random
import time

from config import config

from . import cards as boost_cards
from . import numbers

#: Numbers per importContacts call. The existing contacts job uses 50 and that
#: is proven on this host, so it is not raised here.
BATCH = int(getattr(config, "BOOST_BATCH", 50) or 50)
#: Longest single FLOOD_WAIT worth sleeping through before giving up the run.
MAX_WAIT = int(getattr(config, "MAX_FLOOD_WAIT", 90) or 90)
#: Seconds between batches. The manual contacts job uses CONTACT_CREATE_DELAY,
#: which defaults to 0.2s -- fine when a human is watching, but this runs
#: unattended right after a login, on the account Eitaa is most suspicious of.
#: 400 numbers at 2s per batch of 50 is under a minute either way.
BOOST_DELAY = float(getattr(config, "BOOST_DELAY", 2.0) or 0)
#: How much the probe count wobbles per run, in percent, so every run is not
#: exactly the same size.
JITTER_PCT = max(0, int(getattr(config, "BOOST_JITTER", 10) or 0))


def _jitter(count: int) -> int:
    """`count` give or take JITTER_PCT percent, never below 1."""
    n = max(1, int(count or 0))
    if JITTER_PCT <= 0:
        return n
    span = max(1, n * JITTER_PCT // 100)
    return max(1, n + random.randint(-span, span))


async def boost(driver, account: str, phone: str, *, prefix: str,
                probe: int = 400, report=None, paint=None,
                contacts_before: int | None = None,
                save_peers=None, should_stop=None,
                delay: float | None = None) -> dict:
    """Probe `probe` unused numbers for `account`.

    `paint(text)` is awaited with the live card text (the caller owns the
    message, so the boost can be shown inside the ACCOUNT ADDED card).
    `save_peers(added_rows)` is awaited with the matched users so the caller can
    persist their access_hash. Neither is required.

    Returns a summary dict and never raises for a data problem.
    """
    t0 = time.time()
    stop = should_stop or (lambda: False)
    pace = float(BOOST_DELAY if delay is None else delay)

    state = {
        "ok": False, "reason": None, "prefix": "", "probe_total": int(probe or 0),
        "probed": 0, "matched": 0, "errors": 0, "waited": 0,
        "contacts_before": int(contacts_before or 0), "contacts_after": 0,
        "increase": 0, "peers_new": 0, "peers_total": 0,
        "phone_format": None, "asked": 0, "span": ("", ""),
        "stopped": False, "rate_limited": False, "random": True,
        "shared_range": True, "elapsed": 0.0, "note": None,
    }

    pfx, err = numbers.normalize_prefix(prefix)
    if err:
        state["reason"] = err
        if report is not None:
            await report(boost_cards.skipped(account=account, phone=phone,
                                             reason=err))
        state["elapsed"] = time.time() - t0
        return state
    state["prefix"] = pfx

    state["shared_range"] = numbers.shared_enabled()
    state["random"] = numbers.random_order()
    entries: list[dict] = []
    indices: list[int] = []

    async def show(status: str, step: str, note: str | None = None) -> None:
        if paint is None:
            return
        await paint(boost_cards.progress(
            account=account, phone=phone, prefix=pfx, status=status, step=step,
            probe_total=state["probe_total"], probed=state["probed"],
            matched=state["matched"],
            contacts_before=state["contacts_before"] or None,
            contacts_now=(state["contacts_before"] + state["matched"]) or None,
            phone_format=state["phone_format"], waited=state["waited"],
            random_pick=state["random"], span=state["span"],
            elapsed=time.time() - t0, note=note))

    await show("STARTING", "PREPARING")

    if not await driver.ensure_contacts_bridge():
        # The per-number UI fallback exists in the manual contacts job, but it is
        # minutes of clicking per number -- not something to run unattended
        # behind a login.
        state["reason"] = "the contacts bridge could not be injected"
        if report is not None:
            await report(boost_cards.skipped(account=account, phone=phone,
                                             reason=state["reason"]))
        state["elapsed"] = time.time() - t0
        return state

    # ---- the real "before" number -------------------------------------------
    # Measured, not assumed: this is what makes "Increase" trustworthy.
    if contacts_before is None:
        state["contacts_before"] = await _live_count(driver, default=0)

    # ---- claim the block, as LATE as possible --------------------------------
    # Reserving is what stops two accounts being handed the same numbers, but it
    # also consumes them. So it happens only once everything that could make the
    # run bail out has already been checked -- an unusable bridge used to burn a
    # whole block on the way out.
    # The count is jittered so runs are not all EXACTLY the same size, which is
    # itself a signature. The owner asked for "about 400", and that is what this
    # is: 400 +/- JITTER percent.
    asked = _jitter(probe)
    state["asked"] = asked
    entries, indices, err = numbers.next_numbers(account, pfx, asked,
                                                 first_name=phone)
    if err or not entries:
        state["reason"] = err or "no numbers left under this prefix"
        if report is not None:
            await report(boost_cards.skipped(account=account, phone=phone,
                                             reason=state["reason"]))
        state["elapsed"] = time.time() - t0
        return state
    state["probe_total"] = len(entries)
    state["span"] = (numbers.label(pfx, min(indices)),
                     numbers.label(pfx, max(indices)))

    # ---- phone format: probe once per account, then remember ----------------
    # A wrong format matches NOBODY with no error at all, so both forms have to
    # be tried until one of them matches somebody. But a block where nobody
    # exists also matches nobody in both forms, and trying both on every chunk
    # would double the number of calls for a whole run -- on a brand-new account,
    # which is what PEER_FLOOD punishes fastest. So the double-probe is budgeted
    # to the first few chunks and then the run settles on the documented default.
    known = numbers.phone_format(account)
    formats = [known] if known else ["98", "+98"]
    probe_budget = 0 if known else 2

    batch_size = max(1, BATCH)
    idx = 0
    total = len(entries)
    await show("RUNNING", "IMPORTING")

    while idx < total:
        if stop():
            state["stopped"] = True
            break
        chunk = entries[idx:idx + batch_size]
        matched_here = 0
        used_format = None
        limited = False
        failed = None

        for fmt in formats:
            r = await driver.bridge_import_contacts(
                chunk, plus_prefix=(fmt == "+98"))
            if r.get("limit"):
                limited = True
                wait = int(r.get("wait") or 0)
                state["rate_limited"] = True
                if wait and wait <= MAX_WAIT:
                    state["waited"] += wait
                    await show("WAITING", f"FLOOD - {wait}s",
                               note=f"Eitaa asked for {wait}s; the run continues "
                                    f"after that.")
                    await asyncio.sleep(wait)
                    # Be gentler from here on rather than walking into it again.
                    batch_size = max(10, batch_size // 2)
                else:
                    state["note"] = (f"Eitaa returned {r.get('code')}"
                                     + (f" (wait {wait}s)" if wait else "")
                                     + " -- longer than this run will wait.")
                break
            if not r.get("ok"):
                failed = str(r.get("code"))
                break
            matched_here = int(r.get("imported_count") or 0)
            used_format = fmt
            state["errors"] += int(r.get("retry_count") or 0)
            if matched_here or known:
                # Either it worked, or the format was already proven for this
                # account and a zero simply means nobody in this block exists.
                if save_peers is not None and r.get("added"):
                    # Counted up and reported ONCE on the summary card. Posting a
                    # card per batch meant up to eight "PEERS SAVED" cards for a
                    # single 400-number run.
                    n = await save_peers(r.get("added"))
                    try:
                        state["peers_new"] += int(n or 0)
                    except (TypeError, ValueError):
                        pass
                break
            # Unknown format and zero matches -> try the other form on the SAME
            # numbers before consuming them.

        if limited:
            if state["note"]:
                break          # too long a wait: stop, nothing was consumed
            continue           # slept through it: retry this same chunk
        if failed is not None:
            state["note"] = f"importContacts failed: {failed}"
            break
        if used_format is None:
            # Neither format was accepted at all.
            state["note"] = ("neither the 98 nor the +98 number format was "
                             "accepted by this Eitaa build")
            break

        # ---- consume the numbers, on disk, BEFORE anything is awaited --------
        # Same rule as the send ledger: a stop or a kill arriving at the next
        # await must not leave these numbers looking un-probed, or the following
        # run submits them again and adds nobody.
        numbers.advance(account, pfx, probed=len(chunk), hits=matched_here)
        # Only a format that actually MATCHED somebody is proof. A block where
        # nobody exists answers "imported: 0" in BOTH formats, so remembering the
        # last one tried would pin the account to a format that may well be the
        # wrong one -- forever, since the probe is then skipped.
        if matched_here and not known and used_format:
            numbers.remember_format(account, used_format)
            known = used_format
            formats = [used_format]
            state["phone_format"] = used_format
        elif known:
            state["phone_format"] = known
        elif not known and probe_budget > 0:
            probe_budget -= 1
            if probe_budget == 0:
                # Still nothing. Most likely this block simply holds nobody, so
                # stop paying double and carry on with the form the live account
                # was measured to use. Nothing is remembered: the next run probes
                # both again.
                formats = ["98"]
                state["note"] = (state["note"] or
                                 "no number matched in either format yet, so the "
                                 "run continued in the 98 form only")
        state["matched"] += matched_here
        state["probed"] += len(chunk)
        idx += len(chunk)

        await show("RUNNING", "IMPORTING")
        if idx < total and pace > 0:
            await asyncio.sleep(pace)

    # ---- hand back what was claimed but never submitted ---------------------
    # Numbers are claimed at draw time so two accounts can never be given the
    # same one. A run that stopped early, or that a limit cut short, must return
    # what it never used or those numbers would be lost to everybody.
    if state["probed"] < len(indices):
        state["returned"] = numbers.undraw(account, pfx, indices[state["probed"]:])

    # ---- the real "after" number -------------------------------------------
    await show("VERIFYING", "READING CONTACTS")
    after = await _live_count(driver, default=None, save_cache=account)
    state["contacts_after"] = (after if after is not None
                               else state["contacts_before"] + state["matched"])
    if after is None:
        state["note"] = (state["note"] or
                         "the contact count could not be re-read; the increase "
                         "shown is the server's own tally")
    state["increase"] = max(0, state["contacts_after"] - state["contacts_before"])
    state["ok"] = True
    state["elapsed"] = time.time() - t0
    numbers.advance(account, pfx, probed=0, hits=0, finished_run=True)
    return state


async def _live_count(driver, default=None, save_cache: str | None = None):
    """Read the account's contact count from Eitaa (contacts.getContacts).

    When `save_cache` is given the fresh list also replaces the account's
    contacts cache, so contacts added a moment ago are immediately sendable
    instead of waiting for the next manual 'Update Contacts'.
    """
    try:
        if not await driver.ensure_contacts_list_bridge():
            return default
        res = await driver.bridge_contacts_list()
        if not res or not res.get("ok"):
            return default
        contacts = res.get("contacts") or []
        if save_cache:
            try:
                from bot import contacts_store
                rec = contacts_store.save(save_cache, contacts)
                return int(rec.get("count") or len(contacts))
            except Exception:  # noqa: BLE001 - the count still matters
                pass
        return int(res.get("count") or len(contacts))
    except Exception:  # noqa: BLE001 - never break the caller
        return default


def summary_card(account: str, phone: str, state: dict) -> str:
    """The result card for a finished run."""
    st = numbers.stats(account, state.get("prefix") or "")
    return boost_cards.finished(
        account=account, phone=phone, prefix=state.get("prefix") or "",
        probe_total=int(state.get("probe_total") or 0),
        probed=int(state.get("probed") or 0),
        matched=int(state.get("matched") or 0),
        contacts_before=int(state.get("contacts_before") or 0),
        contacts_after=int(state.get("contacts_after") or 0),
        elapsed=float(state.get("elapsed") or 0.0),
        span=state.get("span") or ("", ""),
        random_pick=bool(state.get("random")),
        shared_range=bool(state.get("shared_range")),
        accounts_served=int(st.get("accounts") or 0),
        phone_format=state.get("phone_format"),
        waited=int(state.get("waited") or 0),
        returned=int(state.get("returned") or 0),
        used_under_prefix=int(st.get("used") or 0),
        left_under_prefix=int(st.get("left") or 0),
        capacity=int(st.get("capacity") or 0),
        lifetime_tried=int(st.get("tried") or 0),
        lifetime_hits=int(st.get("hits") or 0),
        peers_new=int(state.get("peers_new") or 0),
        peers_total=int(state.get("peers_total") or 0),
        stopped=bool(state.get("stopped")),
        rate_limited=bool(state.get("rate_limited")),
        note=state.get("note"),
    )


def enabled() -> bool:
    """Whether Contact Boost may run. The panel setting wins; env is the default.

    Wrapped so an unreadable state file means OFF rather than a crash -- the
    same read-back idiom eitaa/warmpath.py uses.
    """
    try:
        from bot.store import store
        return bool(store.boost)
    except Exception:  # noqa: BLE001
        return bool(getattr(config, "BOOST", False))


def settings() -> tuple[str, int]:
    """(prefix, probe count) from the panel, falling back to the env defaults."""
    prefix = str(getattr(config, "BOOST_PREFIX", "") or "")
    probe = int(getattr(config, "BOOST_PROBE", 400) or 400)
    try:
        from bot.store import store
        prefix = store.boost_prefix or prefix
        probe = int(store.boost_probe or probe)
    except Exception:  # noqa: BLE001
        pass
    return prefix, max(1, probe)
