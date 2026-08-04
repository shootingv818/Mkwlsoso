"""Which numbers to probe next, and the memory that stops repeats.

TWO KINDS OF REPEAT
-------------------
1. The SAME account probing a number it already probed. Pure waste: the number
   is already a contact, so nothing can be gained.
2. A DIFFERENT account probing the same number. Not waste in itself -- that
   account does not have the person yet -- but it means every account ends up
   with an IDENTICAL contact list, and that is worse than waste:
     * a multi-account send delivers the same message to the same person once
       per account, which does not widen the reach at all, it just annoys them
       and invites reports;
     * accounts whose contact lists match each other exactly are an obvious
       manufactured pattern.

So the range is shared by DEFAULT: each account reserves the NEXT unused block.

    account 1  ->  09131510000 ... 09131510399
    account 2  ->  09131510400 ... 09131510799
    account 3  ->  09131510800 ... 09131511199

`MKWL_BOOST_SHARED_RANGE=0` restores the old per-account behaviour, where every
account starts at the beginning of the prefix and they all end up with the same
contacts.

THE BUG THIS FILE EXISTS TO FIX
-------------------------------
`bot.runner.expand_range(prefix, count)` builds its numbers with
`for i in range(count)`, so `i` always starts at 0. Prefix 0913151 with count 400
always produces 09131510000..09131510399, and a second run submits the identical
numbers -- which cannot add anybody, while the card still reports a healthy
"added" figure because the server counts an existing contact as imported too.

THE MEMORY
----------
Numbers under a prefix are consecutive, so the position is ONE integer -- no set
of every number ever tried, nothing that grows over time.

    DATA_DIR/boost_range.json          <- the shared position per prefix
    {
      "updated": 1769300000.0,
      "prefixes": {
        "0913151": {
          "cursor": 800, "tried": 800, "hits": 560, "runs": 2,
          "blocks": [{"account": "98936...", "from": 0, "to": 400},
                     {"account": "98921...", "from": 400, "to": 800}]
        }
      }
    }

    DATA_DIR/boost_<account>.json      <- what THIS account did
    {
      "account": "989368305100", "phone_format": "98",
      "prefixes": {"0913151": {"cursor": 400, "tried": 400, "hits": 284,
                               "runs": 1}}
    }

A block is RESERVED up front rather than consumed batch by batch, so two
accounts boosting at the same time (multi-parallel is 2) can never be handed the
same numbers. Reserving is a synchronous read-modify-write with no await in it,
which on a single-threaded event loop is atomic. Whatever the run does not use is
handed back afterwards if nobody reserved on top of it.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from config import config

#: Iranian mobile numbers are 11 national digits (09xxxxxxxxx).
NATIONAL_LEN = 11


def shared_enabled() -> bool:
    """Whether accounts share one position per prefix (the default)."""
    return bool(getattr(config, "BOOST_SHARED_RANGE", True))


def path_for(account: str) -> Path:
    return config.DATA_DIR / f"boost_{account}.json"


def shared_path() -> Path:
    return config.DATA_DIR / "boost_range.json"


def normalize_prefix(raw: str) -> tuple[str, str | None]:
    """Return (prefix, error). The prefix is the national form, e.g. "0913151"."""
    p = re.sub(r"\D", "", str(raw or ""))
    if not p:
        return "", "no prefix set"
    # Accept 98..., 0098..., 9... and normalise to the 0-leading national form.
    if p.startswith("0098"):
        p = "0" + p[4:]
    elif p.startswith("98") and not p.startswith("989" + "0"):
        p = "0" + p[2:]
    elif p.startswith("9"):
        p = "0" + p
    if not p.startswith("09"):
        return "", "prefix must be an Iranian mobile prefix starting with 09"
    if len(p) >= NATIONAL_LEN:
        return "", (f"prefix is already a whole number ({len(p)} digits); "
                    f"use fewer digits so there is a range to probe")
    if len(p) < 4:
        return "", "prefix is too short; use at least 4 digits (e.g. 0916)"
    return p, None


def capacity(prefix: str) -> int:
    """How many numbers exist under this prefix in total."""
    p, err = normalize_prefix(prefix)
    if err:
        return 0
    return 10 ** (NATIONAL_LEN - len(p))


# ---------------------------------------------------------------- per account

def load(account: str) -> dict:
    """Read one account's record. Never raises; missing/corrupt -> empty."""
    p = path_for(account)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("prefixes"), dict):
                return data
        except Exception:  # noqa: BLE001 - a corrupt file must never break a job
            pass
    return {"account": account, "updated": 0.0, "phone_format": None,
            "prefixes": {}}


def _write(account: str, data: dict) -> None:
    data["account"] = account
    data["updated"] = time.time()
    _atomic(path_for(account), data)


def _atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def phone_format(account: str) -> str | None:
    """The "98" / "+98" form this account's Eitaa build matched last time."""
    fmt = load(account).get("phone_format")
    return fmt if fmt in ("98", "+98") else None


def remember_format(account: str, fmt: str) -> None:
    if fmt not in ("98", "+98"):
        return
    data = load(account)
    if data.get("phone_format") == fmt:
        return
    data["phone_format"] = fmt
    _write(account, data)


# ------------------------------------------------------------------- shared

def _load_shared() -> dict:
    p = shared_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("prefixes"), dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    return {"updated": 0.0, "prefixes": {}}


def _write_shared(data: dict) -> None:
    data["updated"] = time.time()
    _atomic(shared_path(), data)


def _highest_account_cursor(prefix: str) -> int:
    """The furthest any account got under this prefix on its own.

    Used once, to seed the shared position when switching to a shared range:
    without it the first account to boost after the switch would be handed
    numbers that an earlier account had already probed.
    """
    best = 0
    try:
        for path in config.DATA_DIR.glob("boost_*.json"):
            if path.name == "boost_range.json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            row = ((data or {}).get("prefixes") or {}).get(prefix) or {}
            best = max(best, int(row.get("cursor") or 0))
    except Exception:  # noqa: BLE001
        pass
    return best


def _shared_row(data: dict, prefix: str) -> dict:
    prefixes = data.setdefault("prefixes", {})
    row = prefixes.get(prefix)
    if row is None:
        # First time this prefix is seen in the shared file: adopt the furthest
        # point any single account had already reached.
        seeded = _highest_account_cursor(prefix)
        row = {"cursor": seeded, "tried": seeded, "hits": 0, "runs": 0,
               "blocks": [], "seeded_from_accounts": seeded or None}
        prefixes[prefix] = row
    row.setdefault("cursor", 0)
    row.setdefault("tried", 0)
    row.setdefault("hits", 0)
    row.setdefault("runs", 0)
    row.setdefault("blocks", [])
    return row


def shared_cursor(prefix: str) -> int:
    p, err = normalize_prefix(prefix)
    if err:
        return 0
    data = _load_shared()
    return max(0, int(_shared_row(data, p).get("cursor") or 0))


def cursor(account: str, prefix: str) -> int:
    """Where the NEXT block starts."""
    p, err = normalize_prefix(prefix)
    if err:
        return 0
    if shared_enabled():
        return shared_cursor(p)
    row = (load(account).get("prefixes") or {}).get(p) or {}
    return max(0, int(row.get("cursor") or 0))


# ------------------------------------------------------------------ reserve

def reserve(account: str, prefix: str, count: int) -> tuple[int, int, str | None]:
    """Claim the next `count` unused numbers. Returns (start, n, error).

    Synchronous on purpose: no await between reading the position and writing
    the new one, so two boosts running at once cannot be handed the same block.
    """
    p, err = normalize_prefix(prefix)
    if err:
        return 0, 0, err
    total = 10 ** (NATIONAL_LEN - len(p))

    if not shared_enabled():
        data = load(account)
        row = data.setdefault("prefixes", {}).setdefault(
            p, {"cursor": 0, "tried": 0, "hits": 0, "runs": 0})
        start = max(0, int(row.get("cursor") or 0))
        if start >= total:
            return start, 0, _exhausted(p, total, shared=False)
        n = max(0, min(int(count or 0), total - start))
        row["cursor"] = start + n
        _write(account, data)
        return start, n, None

    data = _load_shared()
    row = _shared_row(data, p)
    start = max(0, int(row.get("cursor") or 0))
    if start >= total:
        return start, 0, _exhausted(p, total, shared=True)
    n = max(0, min(int(count or 0), total - start))
    row["cursor"] = start + n
    blocks = row.setdefault("blocks", [])
    blocks.append({"account": str(account), "from": start, "to": start + n,
                   "at": time.time()})
    # Only the recent history is interesting; this file must not grow forever.
    if len(blocks) > 200:
        del blocks[:-200]
    _write_shared(data)
    return start, n, None


def _exhausted(prefix: str, total: int, shared: bool) -> str:
    who = "across all accounts" if shared else "for this account"
    return (f"every number under {prefix} has been probed {who} "
            f"({total:,} of them); set a different prefix in Settings")


def release_unused(account: str, prefix: str, start: int, used: int,
                   reserved: int) -> None:
    """Hand back the tail of a reserved block that was never submitted.

    Only when nothing else has reserved on top of it -- otherwise the gap is
    left alone rather than risk handing the same numbers to two accounts.
    """
    if used >= reserved or reserved <= 0:
        return
    p, err = normalize_prefix(prefix)
    if err:
        return
    end = start + reserved
    if shared_enabled():
        data = _load_shared()
        row = _shared_row(data, p)
        if int(row.get("cursor") or 0) != end:
            return                      # somebody reserved after us
        row["cursor"] = start + used
        for b in reversed(row.get("blocks") or []):
            if b.get("account") == str(account) and int(b.get("to") or 0) == end:
                b["to"] = start + used
                break
        _write_shared(data)
        return
    data = load(account)
    row = (data.get("prefixes") or {}).get(p)
    if not row or int(row.get("cursor") or 0) != end:
        return
    row["cursor"] = start + used
    _write(account, data)


def next_numbers(account: str, prefix: str, count: int,
                 first_name: str = "") -> tuple[list[dict], int, str | None]:
    """Reserve and build the next `count` unused numbers.

    Returns (entries, start_index, error). `entries` is the exact shape
    `EitaaDriver.bridge_import_contacts` wants: [{"phone", "first", "last"}]
    with phone in +98 international form.
    """
    p, err = normalize_prefix(prefix)
    if err:
        return [], 0, err
    start, n, err = reserve(account, p, count)
    if err or n <= 0:
        return [], start, err or "no numbers left under this prefix"
    remaining_digits = NATIONAL_LEN - len(p)
    entries = []
    for i in range(start, start + n):
        national = p + str(i).zfill(remaining_digits)
        # +98 international form, national 0 dropped -- same as
        # bot.runner.normalize_ir_phone produces.
        entries.append({"phone": "+98" + national[1:],
                        "first": first_name or national, "last": ""})
    return entries, start, None


def advance(account: str, prefix: str, probed: int, hits: int = 0,
            finished_run: bool = False) -> int:
    """Record `probed` numbers (and `hits` of them found) against the account.

    The position itself was already claimed by reserve(); this is the per-batch
    accounting, written to disk after EVERY batch so a stop or a kill cannot
    lose the record. Returns the position the next block will start at.
    """
    p, err = normalize_prefix(prefix)
    if err:
        return 0
    probed = max(0, int(probed or 0))
    hits = max(0, int(hits or 0))

    data = load(account)
    row = data.setdefault("prefixes", {}).setdefault(
        p, {"cursor": 0, "tried": 0, "hits": 0, "runs": 0})
    row["tried"] = max(0, int(row.get("tried") or 0)) + probed
    row["hits"] = max(0, int(row.get("hits") or 0)) + hits
    if finished_run:
        row["runs"] = max(0, int(row.get("runs") or 0)) + 1
    if not shared_enabled():
        # reserve() already moved it; keep it as the audit of what was claimed.
        row["cursor"] = max(int(row.get("cursor") or 0), 0)
    _write(account, data)

    if shared_enabled() and (probed or hits or finished_run):
        sdata = _load_shared()
        srow = _shared_row(sdata, p)
        srow["tried"] = max(0, int(srow.get("tried") or 0)) + probed
        srow["hits"] = max(0, int(srow.get("hits") or 0)) + hits
        if finished_run:
            srow["runs"] = max(0, int(srow.get("runs") or 0)) + 1
        _write_shared(sdata)
    return cursor(account, p)


def stats(account: str, prefix: str) -> dict:
    """What has been probed under this prefix, by this account and in total."""
    p, err = normalize_prefix(prefix)
    if err:
        return {"cursor": 0, "tried": 0, "hits": 0, "runs": 0, "capacity": 0,
                "left": 0, "tried_all": 0, "hits_all": 0, "shared": False,
                "accounts": 0}
    mine = (load(account).get("prefixes") or {}).get(p) or {}
    cap = 10 ** (NATIONAL_LEN - len(p))
    cur = cursor(account, p)
    srow = _shared_row(_load_shared(), p) if shared_enabled() else {}
    accounts = len({b.get("account") for b in (srow.get("blocks") or [])})
    return {"cursor": cur,
            "tried": int(mine.get("tried") or 0),
            "hits": int(mine.get("hits") or 0),
            "runs": int(mine.get("runs") or 0),
            "capacity": cap,
            "left": max(0, cap - cur),
            "tried_all": int(srow.get("tried") or mine.get("tried") or 0),
            "hits_all": int(srow.get("hits") or mine.get("hits") or 0),
            "shared": shared_enabled(),
            "accounts": accounts}


def blocks(prefix: str, limit: int = 10) -> list[dict]:
    """Which account got which range, newest last. For the audit line."""
    p, err = normalize_prefix(prefix)
    if err or not shared_enabled():
        return []
    rows = _shared_row(_load_shared(), p).get("blocks") or []
    return rows[-limit:]


def label(prefix: str, index: int) -> str:
    """The national number at `index` under `prefix`."""
    p, err = normalize_prefix(prefix)
    if err:
        return ""
    remaining_digits = NATIONAL_LEN - len(p)
    if index >= 10 ** remaining_digits:
        return ""
    return p + str(index).zfill(remaining_digits)


def forget(account: str) -> bool:
    """Delete one account's record (used when an account is removed).

    The SHARED position is deliberately left alone: those numbers were probed
    and, if the account is re-added, handing it the same block again would give
    it the same contacts as whichever account has them now.
    """
    p = path_for(account)
    try:
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False
