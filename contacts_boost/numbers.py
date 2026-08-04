"""Which numbers to probe next, and the memory that stops repeats.

NUMBERS ARE PICKED AT RANDOM
----------------------------
A prefix is a space of numbers -- 0913151 holds 10,000, 09131 holds 100,000,
0913 holds 1,000,000 -- and there is no reason to walk it in order. Random is
better for three separate reasons:

  * Sequential imports are an obvious fingerprint. A real person's contacts are
    not 09131510000, 09131510001, 09131510002 ... in a row. Anybody looking at
    the list can tell it was generated.
  * Consecutive numbers are often sold as a batch to one office or family, so a
    sequential block is an odd-looking cluster on top of being sequential.
  * A sequential walk can land inside a dead sub-block and return almost nobody,
    while a random sample over the whole prefix always returns the prefix's
    average density. It makes the choice of prefix far more forgiving.

THE MEMORY
----------
Random alone is not enough: without memory, two runs would collide constantly
(400 draws out of 10,000 collide with near-certainty). So every index that has
been handed out is remembered, and draws exclude it.

    DATA_DIR/boost_range.json          <- shared by all accounts (the default)
    {
      "prefixes": {
        "0913151": {
          "used": ["0-399", "1043", "2871-2872", ...],
          "tried": 400, "hits": 284, "runs": 1,
          "draws": [{"account": "98936...", "n": 400, "at": 1769...}]
        }
      }
    }

`used` is stored as compact ranges rather than one entry per number, so the
sequential block an earlier version handed out collapses to a single "0-399" and
the file stays readable and small.

The set is SHARED across accounts by default, so no two accounts are ever given
the same number and their contact lists cannot end up identical.
`MKWL_BOOST_SHARED_RANGE=0` gives each account its own set instead.

Indices are claimed at DRAW time -- one synchronous read-modify-write with no
await in it, so two boosts running at once (multi-parallel is 2) can never be
handed the same number. Whatever a run does not submit is handed straight back.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

from config import config

#: Iranian mobile numbers are 11 national digits (09xxxxxxxxx).
NATIONAL_LEN = 11
#: Above this, unused indices are not enumerated (rejection sampling only).
_ENUMERATE_LIMIT = 2_000_000


def shared_enabled() -> bool:
    """Whether accounts share one used-set per prefix (the default)."""
    return bool(getattr(config, "BOOST_SHARED_RANGE", True))


def random_order() -> bool:
    """Whether numbers are picked at random (the default) or in order."""
    return str(getattr(config, "BOOST_ORDER", "random")).lower() != "sequential"


def path_for(account: str) -> Path:
    return config.DATA_DIR / f"boost_{account}.json"


def shared_path() -> Path:
    return config.DATA_DIR / "boost_range.json"


def normalize_prefix(raw: str) -> tuple[str, str | None]:
    """Return (prefix, error). The prefix is the national form, e.g. "0913151"."""
    p = re.sub(r"\D", "", str(raw or ""))
    if not p:
        return "", "no prefix set"
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
                    f"use fewer digits so there is a range to pick from")
    if len(p) < 4:
        return "", "prefix is too short; use at least 4 digits (e.g. 0916)"
    return p, None


def capacity(prefix: str) -> int:
    """How many numbers exist under this prefix in total."""
    p, err = normalize_prefix(prefix)
    return 0 if err else 10 ** (NATIONAL_LEN - len(p))


def label(prefix: str, index: int) -> str:
    """The national number at `index` under `prefix`."""
    p, err = normalize_prefix(prefix)
    if err:
        return ""
    width = NATIONAL_LEN - len(p)
    if index < 0 or index >= 10 ** width:
        return ""
    return p + str(index).zfill(width)


def to_phone(prefix: str, index: int) -> str:
    """The +98 international form Eitaa is given."""
    national = label(prefix, index)
    return ("+98" + national[1:]) if national else ""


# ------------------------------------------------- used-set (packed as ranges)

def _pack(used: set[int]) -> list[str]:
    """Collapse a set of indices into "a-b" / "a" strings, ascending."""
    out: list[str] = []
    if not used:
        return out
    ordered = sorted(used)
    start = prev = ordered[0]
    for n in ordered[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return out


def _unpack(rows) -> set[int]:
    used: set[int] = set()
    for row in rows or []:
        try:
            text = str(row)
            if "-" in text:
                a, b = text.split("-", 1)
                lo, hi = int(a), int(b)
                if hi - lo > 5_000_000:      # refuse an absurd range
                    continue
                used.update(range(lo, hi + 1))
            else:
                used.add(int(text))
        except (TypeError, ValueError):
            continue
    return used


# --------------------------------------------------------------- file access

def _read(path: Path, fallback: dict) -> dict:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("prefixes"), dict):
                return data
        except Exception:  # noqa: BLE001 - a corrupt file must never break a job
            pass
    return dict(fallback)


def _atomic(path: Path, data: dict) -> None:
    data["updated"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def load(account: str) -> dict:
    """One account's record (phone format, its own stats, and its used-set when
    the shared set is switched off)."""
    return _read(path_for(account),
                 {"account": account, "updated": 0.0, "phone_format": None,
                  "prefixes": {}})


def _write(account: str, data: dict) -> None:
    data["account"] = account
    _atomic(path_for(account), data)


def _load_shared() -> dict:
    return _read(shared_path(), {"updated": 0.0, "prefixes": {}})


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


# ------------------------------------------------------------ the used-set row

def _legacy_used(prefix: str) -> set[int]:
    """Convert the OLD sequential `cursor` records into used indices.

    Earlier versions handed out numbers 0..cursor in order and stored only the
    position. Those numbers really were used, so they are folded into the set --
    otherwise the first random draw after an upgrade could hand an account
    numbers another account already holds.
    """
    used: set[int] = set()
    try:
        for path in config.DATA_DIR.glob("boost_*.json"):
            if path.name == "boost_range.json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            row = ((data or {}).get("prefixes") or {}).get(prefix) or {}
            cur = int(row.get("cursor") or 0)
            if cur > 0:
                used.update(range(0, min(cur, 5_000_000)))
    except Exception:  # noqa: BLE001
        pass
    return used


def _row(data: dict, prefix: str, *, shared: bool) -> dict:
    prefixes = data.setdefault("prefixes", {})
    row = prefixes.get(prefix)
    if row is None:
        row = {"used": [], "tried": 0, "hits": 0, "runs": 0, "draws": []}
        if shared:
            legacy = _legacy_used(prefix)
            if legacy:
                row["used"] = _pack(legacy)
                row["tried"] = len(legacy)
                row["migrated_from_sequential"] = len(legacy)
        prefixes[prefix] = row
    row.setdefault("used", [])
    for k in ("tried", "hits", "runs"):
        row.setdefault(k, 0)
    row.setdefault("draws", [])
    return row


def _store_for(account: str) -> tuple[dict, Path, bool]:
    if shared_enabled():
        return _load_shared(), shared_path(), True
    return load(account), path_for(account), False


def used_count(account: str, prefix: str) -> int:
    p, err = normalize_prefix(prefix)
    if err:
        return 0
    data, _, shared = _store_for(account)
    return len(_unpack(_row(data, p, shared=shared).get("used")))


# ------------------------------------------------------------------- drawing

def draw(account: str, prefix: str, count: int) -> tuple[list[int], str | None]:
    """Claim `count` unused indices and return them. Never repeats.

    Synchronous on purpose: nothing is awaited between reading the used-set and
    writing it back, so two boosts running at the same moment cannot be handed
    the same number.
    """
    p, err = normalize_prefix(prefix)
    if err:
        return [], err
    total = 10 ** (NATIONAL_LEN - len(p))
    want = max(0, int(count or 0))
    if want <= 0:
        return [], "nothing to probe"

    data, path, shared = _store_for(account)
    row = _row(data, p, shared=shared)
    used = _unpack(row.get("used"))
    left = total - len(used)
    if left <= 0:
        who = "across all accounts" if shared else "for this account"
        return [], (f"every number under {p} has been probed {who} "
                    f"({total:,} of them); set a different prefix in Settings")
    want = min(want, left)

    picked = (_pick_sequential(used, total, want) if not random_order()
              else _pick_random(used, total, want))
    if not picked:
        return [], "could not find unused numbers under this prefix"

    used.update(picked)
    row["used"] = _pack(used)
    draws = row.setdefault("draws", [])
    draws.append({"account": str(account), "n": len(picked), "at": time.time()})
    if len(draws) > 200:
        del draws[:-200]
    if shared:
        _atomic(path, data)
    else:
        _write(account, data)
    # Submitted in the order drawn, which for a random draw is already scattered.
    return picked, None


def _pick_random(used: set[int], total: int, want: int) -> list[int]:
    """Rejection sampling, with an exact fallback once the space gets dense."""
    picked: list[int] = []
    seen: set[int] = set()
    # 20 tries per number is plenty while under ~90% density; past that the
    # fallback below is both faster and exact.
    budget = want * 20
    while len(picked) < want and budget > 0:
        budget -= 1
        r = random.randrange(total)
        if r in used or r in seen:
            continue
        seen.add(r)
        picked.append(r)
    if len(picked) >= want or total > _ENUMERATE_LIMIT:
        return picked
    # Dense: list what is actually free and sample from that.
    free = [i for i in range(total) if i not in used and i not in seen]
    random.shuffle(free)
    picked.extend(free[:want - len(picked)])
    return picked


def _pick_sequential(used: set[int], total: int, want: int) -> list[int]:
    """The old behaviour, kept for MKWL_BOOST_ORDER=sequential."""
    picked: list[int] = []
    i = 0
    while i < total and len(picked) < want:
        if i not in used:
            picked.append(i)
        i += 1
    return picked


def undraw(account: str, prefix: str, indices) -> int:
    """Hand back indices that were claimed but never submitted."""
    give_back = {int(i) for i in (indices or [])}
    if not give_back:
        return 0
    p, err = normalize_prefix(prefix)
    if err:
        return 0
    data, path, shared = _store_for(account)
    row = _row(data, p, shared=shared)
    used = _unpack(row.get("used"))
    freed = len(used & give_back)
    if not freed:
        return 0
    row["used"] = _pack(used - give_back)
    if shared:
        _atomic(path, data)
    else:
        _write(account, data)
    return freed


def next_numbers(account: str, prefix: str, count: int,
                 first_name: str = "") -> tuple[list[dict], list[int], str | None]:
    """Draw and build the next `count` numbers.

    Returns (entries, indices, error). `entries` is the exact shape
    `EitaaDriver.bridge_import_contacts` wants, in the same order as `indices`.
    """
    p, err = normalize_prefix(prefix)
    if err:
        return [], [], err
    indices, err = draw(account, p, count)
    if err or not indices:
        return [], [], err or "no numbers left under this prefix"
    entries = []
    for i in indices:
        national = label(p, i)
        entries.append({"phone": "+98" + national[1:],
                        "first": first_name or national, "last": ""})
    return entries, indices, None


# ------------------------------------------------------------------ counting

def advance(account: str, prefix: str, probed: int, hits: int = 0,
            finished_run: bool = False) -> None:
    """Record what a batch actually did.

    The numbers themselves were claimed by draw(); this is the accounting, and it
    is written after EVERY batch so a stop or a kill cannot lose it.
    """
    p, err = normalize_prefix(prefix)
    if err:
        return
    probed = max(0, int(probed or 0))
    hits = max(0, int(hits or 0))

    mine = load(account)
    row = mine.setdefault("prefixes", {}).setdefault(
        p, {"tried": 0, "hits": 0, "runs": 0})
    row["tried"] = max(0, int(row.get("tried") or 0)) + probed
    row["hits"] = max(0, int(row.get("hits") or 0)) + hits
    if finished_run:
        row["runs"] = max(0, int(row.get("runs") or 0)) + 1
    _write(account, mine)

    if shared_enabled() and (probed or hits or finished_run):
        data = _load_shared()
        srow = _row(data, p, shared=True)
        srow["hits"] = max(0, int(srow.get("hits") or 0)) + hits
        if finished_run:
            srow["runs"] = max(0, int(srow.get("runs") or 0)) + 1
        _atomic(shared_path(), data)


def stats(account: str, prefix: str) -> dict:
    """What has been probed under this prefix, by this account and in total."""
    p, err = normalize_prefix(prefix)
    if err:
        return {"tried": 0, "hits": 0, "runs": 0, "capacity": 0, "left": 0,
                "used": 0, "hits_all": 0, "shared": False, "accounts": 0,
                "random": True}
    mine = (load(account).get("prefixes") or {}).get(p) or {}
    cap = 10 ** (NATIONAL_LEN - len(p))
    data, _, shared = _store_for(account)
    row = _row(data, p, shared=shared)
    used = len(_unpack(row.get("used")))
    accounts = len({d.get("account") for d in (row.get("draws") or [])})
    return {"tried": int(mine.get("tried") or 0),
            "hits": int(mine.get("hits") or 0),
            "runs": int(mine.get("runs") or 0),
            "capacity": cap,
            "used": used,
            "left": max(0, cap - used),
            "hits_all": int(row.get("hits") or 0),
            "shared": shared,
            "accounts": accounts,
            "random": random_order()}


def draws(prefix: str, limit: int = 10) -> list[dict]:
    """Which account drew how many, newest last."""
    p, err = normalize_prefix(prefix)
    if err or not shared_enabled():
        return []
    return (_row(_load_shared(), p, shared=True).get("draws") or [])[-limit:]


def forget(account: str) -> bool:
    """Delete one account's record (used when an account is removed).

    The SHARED used-set is deliberately left alone: those numbers were handed
    out, and if the account is re-added, drawing them again would give it the
    same contacts as whichever account holds them now.
    """
    p = path_for(account)
    try:
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False
