"""Broadcast send order, built on what Eitaa ACTUALLY reports.

THE MEASUREMENT THIS IS BUILT ON
--------------------------------
A live census of 1,006 contacts (deploy/contacts_status.py, then
deploy/census_gap_check.py) established two facts that decide this whole design:

  1. Eitaa returns an exact `was_online` timestamp ONLY for activity inside the
     last 24 hours. Of 245 contacts carrying a was_online, 209 were genuine and
     EVERY ONE of them fell within 23.1 hours. Beyond that window Eitaa stops
     giving times and reports a coarse category instead
     (userStatusRecently / LastWeek / LastMonth / Empty).

  2. The other 36 carried `was_online = 0` -- all of them exactly zero, not
     merely small. Zero is not a date. It is Eitaa declining to give one. The
     previous tiering aged it against now, got ~56 years, and invented a
     "seen more than a month ago" tier with 36 phantom members. Worse, that
     phantom tier sat ABOVE the genuinely-recent contacts in the send order.

So the timestamp is not the organising principle -- Eitaa's own status category
is. The timestamp is used for exactly one thing: splitting "online right now"
from "seen within 24h", which is the only range where a timestamp exists. Tiers
like "seen 3 days ago" cannot be built from this data on ANY account, no matter
what privacy settings people use, so they are not offered.

THE ORDER
---------
    1. online          online right now
    2. today           exact timestamp inside 24h  (the only exact tier)
    3. recently         Eitaa says "recently", no time
    4. week_or_month    Eitaa says "last week" or "last month", no time
    5. long_ago         no usable signal at all

Merging "last week" with "last month" is deliberate: both are Eitaa's own coarse
buckets with no timestamp behind them, so ranking one above the other would be
inventing precision that the data does not contain.

WHAT long_ago DELIBERATELY MIXES, AND WHY IT STILL RECORDS THE DIFFERENCE
------------------------------------------------------------------------
long_ago holds several distinct facts: userStatusEmpty, the was_online = 0
sentinel, and a missing status field. They are worth the same for SENDING -- all
mean "no idea, go last" -- so splitting the send order on them would add tiers
without adding information. But they are NOT the same fact, so every contact
keeps a `reason` recording which one it was. Nothing is thrown away; the
distinction just does not drive the order.

BEHAVIOUR-CHANGE DETECTION
--------------------------
Everything above rests on Eitaa's observed 24-hour cutoff. If that changes, this
design quietly degrades instead of failing, which is the dangerous kind of
breakage. So a genuine timestamp OLDER than 24h is counted as `exact_stale` and
raises a warning rather than being silently absorbed: it means the premise no
longer holds and the tiers should be revisited.

THE ORDER CANNOT BE BUILT FROM THE CONTACTS CACHE
-------------------------------------------------
bot/contacts_store.save() whitelists {title, peer_id, access_hash}, so status
never reaches the cache -- and that is correct, because status is the most
volatile thing on the wire. Someone "online" is offline minutes later. Tiering a
broadcast off a cached file would order it by who was online whenever the cache
was last refreshed, which could be weeks ago, while looking perfectly healthy.
Always build the order from a fresh getContacts immediately before sending.

Pure functions, no browser, no network, stdlib only -- which is what makes the
whole thing testable without an account (bot/tests/test_send_order.py).
"""
from __future__ import annotations

import time

#: Eitaa hands out an exact was_online only inside this window. Measured, not
#: assumed: the oldest genuine timestamp in a 1,006-contact census was 23.1h.
EXACT_WINDOW_SEC = 86_400

#: Grace band above EXACT_WINDOW_SEC. A timestamp landing here is a contact that
#: drifted over the 24h edge since the list was fetched -- expected, not a fault.
#: Only a timestamp beyond this band contradicts the measurement.
COARSE_WINDOW_SEC = 30 * 86_400

#: was_online below this is a sentinel meaning "no time given", not a date. No
#: real account was last seen on 1970-01-02, and the observed values were all
#: exactly 0. Kept as a window rather than `== 0` so a near-epoch variant is
#: caught too.
EPOCH_SENTINEL_CUTOFF = 86_400

#: Send order, best signal first. The list IS the order; index position is what
#: sorting uses, so reordering here reorders a broadcast.
TIERS: list[tuple[str, str]] = [
    ("online", "online right now"),
    ("today", "seen within 24h (exact timestamp)"),
    ("recently", "Eitaa says 'recently' (no time given)"),
    ("week_or_month", "Eitaa says 'last week' or 'last month' (no time given)"),
    ("long_ago", "no usable last-seen signal"),
]

TIER_KEYS: list[str] = [k for k, _ in TIERS]
TIER_INDEX: dict[str, int] = {k: i for i, k in enumerate(TIER_KEYS)}
TIER_LABEL: dict[str, str] = dict(TIERS)

#: Reasons that mean the premise of this design no longer holds. Surfaced as
#: warnings instead of being absorbed silently.
#:
#: `exact_over_24h` is deliberately NOT here. It fires on ordinary boundary
#: drift, and a warning that cries wolf on every run trains you to ignore the
#: one that matters.
ALARMING_REASONS = ("exact_very_old",)


def classify(status: str | None, was_online=None, expires=None,
             now: int | None = None) -> tuple[str, str]:
    """Map one Eitaa status to (tier, reason).

    `status` is the raw constructor name straight from the wire
    ("userStatusOnline", "userStatusRecently", ...). Deciding on Eitaa's own
    category and not on a derived age is the entire point: the category is
    always present, the age usually is not.

    `reason` is finer-grained than `tier` on purpose -- several different facts
    share a tier, and the reason is what keeps them distinguishable afterwards.
    """
    now = int(now if now is not None else time.time())
    name = (status or "").strip()

    if not name:
        return "long_ago", "no_status"

    if name == "userStatusOnline":
        # Eitaa saying userStatusOnline IS the statement that they are online.
        # `expires` is NOT used to second-guess it.
        #
        # An earlier version demoted a contact to tier 2 when `expires` had
        # already passed, reasoning that the record was stale. On the live
        # account that emptied tier 1 completely: all 9 online contacts came
        # back with an expires in the past and were demoted, so the tier the
        # whole feature exists for had zero members. `expires` is compared
        # against OUR host clock, and comparing a server's assertion against a
        # clock we do not control is exactly the "chase the timestamp" mistake
        # this module was written to avoid. The count of past-expiry records is
        # reported by build_order as a clock observation instead.
        return "online", "online"

    if name == "userStatusOffline":
        if not isinstance(was_online, (int, float)):
            return "long_ago", "offline_no_time"
        ts = int(was_online)
        if ts < EPOCH_SENTINEL_CUTOFF:
            return "long_ago", "sentinel_zero"
        age = now - ts
        if age < 0:
            # Clock skew between the server's now and ours. Seen "in the
            # future" means seen essentially now, not long ago.
            return "today", "clock_skew"
        if age <= EXACT_WINDOW_SEC:
            return "today", "exact_within_24h"
        if age <= COARSE_WINDOW_SEC:
            # Just past the 24h edge. This is NORMAL and was originally
            # mishandled twice over: first it was treated as an alarm, and
            # second it was filed under long_ago, whose label is "no usable
            # last-seen signal" -- which is plainly false for a contact whose
            # exact last-seen time we know.
            #
            # It appears because contacts drift across the boundary between
            # runs: a census at 19:59 showed 209 contacts inside 24h, and a
            # rerun 65 minutes later showed 208 plus exactly one past the edge.
            # Nothing about Eitaa changed; one person's timestamp simply aged
            # out. The tier below is where Eitaa's own days-to-weeks contacts
            # sit, so that is where this belongs.
            return "week_or_month", "exact_over_24h"
        # Weeks or months old WITH an exact timestamp. Eitaa is not supposed to
        # give times this old at all, so this is the reading that would mean the
        # 24-hour premise genuinely changed.
        return "long_ago", "exact_very_old"

    if name == "userStatusRecently":
        return "recently", "recently"
    if name == "userStatusLastWeek":
        return "week_or_month", "last_week"
    if name == "userStatusLastMonth":
        return "week_or_month", "last_month"
    if name == "userStatusEmpty":
        return "long_ago", "empty"

    # A constructor we have never seen. Sending last is the safe default, but it
    # must be reported, not swallowed.
    return "long_ago", f"unknown:{name[:40]}"


def _sort_rank(tier: str, was_online) -> int:
    """Within-tier ordering. Only meaningful where a timestamp exists.

    Returning 0 for the timeless tiers keeps Python's stable sort from
    reshuffling them, so the API's own order survives and two runs on the same
    data produce the same order.
    """
    if tier == "today" and isinstance(was_online, (int, float)):
        return -int(was_online)          # most recently seen first
    return 0


def build_order(contacts: list[dict], now: int | None = None) -> dict:
    """Order contacts for a broadcast. Returns the plan plus why it looks so.

    Each entry keeps its original fields and gains `tier` and `reason`. Input is
    never mutated -- the caller may still hold the raw list.

    A contact with no peer_id is dropped and counted: peer_id is what the fast
    in-page send addresses, so an entry without one cannot be delivered to and
    including it would inflate every tier.
    """
    now = int(now if now is not None else time.time())

    decorated: list[tuple[int, int, int, dict]] = []
    tier_counts = {k: 0 for k in TIER_KEYS}
    reason_counts: dict[str, int] = {}
    dropped_no_peer = 0
    # Clock evidence. `now` is the browser's Date.now(), i.e. THIS host's clock,
    # not Eitaa's. Eitaa sets an online contact's `expires` a few minutes into
    # its own future, so an expires that looks past to us means our clock is
    # ahead. Collected because it is real information about the box, and because
    # it is the evidence for why `expires` is not used to place anyone.
    expiry_past = 0
    expiry_past_max = 0
    stale_max_age = 0

    for seq, raw in enumerate(contacts or []):
        if not isinstance(raw, dict):
            dropped_no_peer += 1
            continue
        peer_id = raw.get("peer_id")
        if peer_id in (None, ""):
            dropped_no_peer += 1
            continue

        was_online = raw.get("was_online")
        expires = raw.get("expires")
        tier, reason = classify(raw.get("status"), was_online=was_online,
                                expires=expires, now=now)

        if reason == "online" and isinstance(expires, (int, float)):
            behind = now - int(expires)
            if behind > 0:
                expiry_past += 1
                expiry_past_max = max(expiry_past_max, behind)
        if reason in ("exact_over_24h", "exact_very_old"):
            stale_max_age = max(stale_max_age, now - int(was_online))
        entry = dict(raw)
        entry["tier"] = tier
        entry["reason"] = reason

        tier_counts[tier] += 1
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        # seq is the tie-breaker that makes the sort fully deterministic.
        decorated.append((TIER_INDEX[tier], _sort_rank(tier, was_online), seq, entry))

    decorated.sort(key=lambda t: (t[0], t[1], t[2]))
    ordered = [t[3] for t in decorated]

    warnings: list[str] = []
    observations: list[str] = []

    if expiry_past:
        observations.append(
            f"{expiry_past} online contact(s) reported an `expires` already in "
            f"the past, by up to {_dur(expiry_past_max)}. This measures how STALE "
            f"the status data is, not clock skew: across three runs the amount "
            f"grew from 6m to 1h9m while the newest last-seen stayed frozen at "
            f"19:54, and a clock offset cannot grow. Run "
            f"deploy/status_freshness.py to see whether fresher data is "
            f"reachable. They stay in tier 1 either way, because Eitaa's own "
            f"category is all we have.")
    if reason_counts.get("exact_over_24h"):
        observations.append(
            f"{reason_counts['exact_over_24h']} contact(s) sit just past the 24h "
            f"edge (oldest {_dur(stale_max_age)}). Expected: contacts age across "
            f"the boundary between runs. Filed under week_or_month, not long_ago, "
            f"since their last-seen time IS known.")

    for reason, n in sorted(reason_counts.items()):
        if reason in ALARMING_REASONS:
            warnings.append(
                f"{n} contact(s) have an exact was_online far past the 24-hour "
                f"window (oldest {_dur(stale_max_age)}). Eitaa is not supposed "
                f"to give times that old at all, so the premise these tiers rest "
                f"on has changed -- revisit EXACT_WINDOW_SEC and the tier list.")
        elif reason.startswith("unknown:"):
            warnings.append(
                f"{n} contact(s) reported an unrecognised status "
                f"'{reason.split(':', 1)[1]}'. They were sent last as a safe "
                f"default, but the mapping in classify() needs updating.")
    if dropped_no_peer:
        warnings.append(
            f"{dropped_no_peer} contact(s) had no peer_id and cannot be "
            f"addressed; they are excluded from every tier.")

    return {
        "now": now,
        "ordered": ordered,
        "total": len(ordered),
        "tier_counts": tier_counts,
        "reason_counts": reason_counts,
        "dropped_no_peer": dropped_no_peer,
        "warnings": warnings,
        "observations": observations,
        "clock": {"online_expires_in_past": expiry_past,
                  "online_expires_behind_max_sec": expiry_past_max},
    }


def _dur(seconds: int) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def groups(plan: dict, how_many: int = 3) -> list[dict]:
    """Collapse the five tiers into coarse send groups with a pause between.

    Three groups is the default because it matches where the data actually
    changes character: proven-or-probably active, Eitaa's coarse middle, and
    nothing-known. Two is offered for when the extra pause is not worth it.
    """
    if how_many == 2:
        shape = [("active", ("online", "today", "recently")),
                 ("rest", ("week_or_month", "long_ago"))]
    elif how_many == 3:
        shape = [("active", ("online", "today", "recently")),
                 ("coarse", ("week_or_month",)),
                 ("unknown", ("long_ago",))]
    else:
        raise ValueError("how_many must be 2 or 3")

    counts = plan["tier_counts"]
    out = []
    for name, keys in shape:
        out.append({"group": name, "tiers": list(keys),
                    "count": sum(counts.get(k, 0) for k in keys)})
    return out
