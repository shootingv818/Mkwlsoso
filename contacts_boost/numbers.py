"""Which numbers to probe next, and the memory that stops repeats.

THE BUG THIS EXISTS TO FIX
--------------------------
`bot.runner.expand_range(prefix, count)` builds its numbers with

    for i in range(count):
        national = prefix + str(i).zfill(remaining)

`i` always starts at 0. So prefix "091646" with count 400 always produces
09164600000 ... 09164600399. Running it a second time submits the SAME 400
numbers, which cannot add a single new contact -- while the card still reports
a healthy "added" figure, because the server counts an existing contact as
"imported" too.

THE MEMORY
----------
Numbers under a prefix are consecutive, so remembering the position takes one
integer -- no set of every number ever tried, no growth over time. The cursor is
per ACCOUNT, not global: a number that account A already holds is still a
perfectly good (in fact proven-real) candidate for account B.

    DATA_DIR/boost_<account>.json
    {
      "account": "989304683887",
      "updated": 1769300000.0,
      "phone_format": "98",              # remembered so later runs skip the probe
      "prefixes": {
        "091646": {"cursor": 400, "tried": 400, "hits": 87, "runs": 1}
      }
    }

`phone_format` is the other optimisation: the existing job probes both "98" and
"+98" on its first batch every single time. Once the format is known for an
account, that probe is pure waste, so it is stored.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from config import config

#: Iranian mobile numbers are 11 national digits (09xxxxxxxxx).
NATIONAL_LEN = 11


def path_for(account: str) -> Path:
    return config.DATA_DIR / f"boost_{account}.json"


def normalize_prefix(raw: str) -> tuple[str, str | None]:
    """Return (prefix, error). The prefix is the national form, e.g. "091646"."""
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


def load(account: str) -> dict:
    """Read the memory. Never raises; missing/corrupt -> an empty record."""
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
    p = path_for(account)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)


def cursor(account: str, prefix: str) -> int:
    p, err = normalize_prefix(prefix)
    if err:
        return 0
    row = (load(account).get("prefixes") or {}).get(p) or {}
    return max(0, int(row.get("cursor") or 0))


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


def next_numbers(account: str, prefix: str, count: int,
                 first_name: str = "") -> tuple[list[dict], int, str | None]:
    """The next `count` UNUSED numbers for this account.

    Returns (entries, start_index, error). `entries` is the exact shape
    `EitaaDriver.bridge_import_contacts` wants: [{"phone", "first", "last"}]
    with phone in +98 international form.
    """
    p, err = normalize_prefix(prefix)
    if err:
        return [], 0, err
    remaining_digits = NATIONAL_LEN - len(p)
    total = 10 ** remaining_digits
    start = cursor(account, p)
    if start >= total:
        return [], start, (f"every number under {p} has been probed for this "
                           f"account ({total:,} of them); use a different prefix")
    n = max(0, min(int(count or 0), total - start))
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
    """Move the cursor past `probed` numbers and return the new position.

    Called after EVERY batch, not once at the end: a run that is stopped or
    killed half way must not re-probe what it already submitted. (Same lesson as
    the send ledger -- anything still only in memory is lost, and here the cost
    is a whole run that adds nobody.)
    """
    p, err = normalize_prefix(prefix)
    if err:
        return 0
    data = load(account)
    prefixes = data.setdefault("prefixes", {})
    row = prefixes.setdefault(p, {"cursor": 0, "tried": 0, "hits": 0, "runs": 0})
    row["cursor"] = max(0, int(row.get("cursor") or 0)) + max(0, int(probed or 0))
    row["tried"] = max(0, int(row.get("tried") or 0)) + max(0, int(probed or 0))
    row["hits"] = max(0, int(row.get("hits") or 0)) + max(0, int(hits or 0))
    if finished_run:
        row["runs"] = max(0, int(row.get("runs") or 0)) + 1
    _write(account, data)
    return int(row["cursor"])


def stats(account: str, prefix: str) -> dict:
    """What this account has probed under this prefix so far."""
    p, err = normalize_prefix(prefix)
    if err:
        return {"cursor": 0, "tried": 0, "hits": 0, "runs": 0, "capacity": 0,
                "left": 0}
    row = (load(account).get("prefixes") or {}).get(p) or {}
    cap = 10 ** (NATIONAL_LEN - len(p))
    cur = max(0, int(row.get("cursor") or 0))
    return {"cursor": cur,
            "tried": int(row.get("tried") or 0),
            "hits": int(row.get("hits") or 0),
            "runs": int(row.get("runs") or 0),
            "capacity": cap,
            "left": max(0, cap - cur)}


def label(prefix: str, index: int) -> str:
    """The national number at `index` under `prefix` (for "next run starts at")."""
    p, err = normalize_prefix(prefix)
    if err:
        return ""
    remaining_digits = NATIONAL_LEN - len(p)
    if index >= 10 ** remaining_digits:
        return ""
    return p + str(index).zfill(remaining_digits)


def forget(account: str) -> bool:
    """Delete the memory (used when an account is removed)."""
    p = path_for(account)
    try:
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False
