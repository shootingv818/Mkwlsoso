#!/usr/bin/env python3
"""Explain the 29-day hole in the contact status census. Read-only, no browser.

WHY THIS EXISTS
---------------
contacts_status.py reported an impossible shape: 209 contacts seen inside 24
hours, 36 seen "more than a month ago", and NOBODY in the 29 days between. A
gap that clean is almost never real data.

The suspect is bucket() in contacts_status.py. It trusts every was_online value
it is handed:

    for ts in (res.get("was_online") or []):
        age = now - int(ts)
        if age < 0:
            age = 0            # clock skew is handled...
        ...
        else:
            out["older"] += 1  # ...but was_online == 0 lands HERE

A userStatusOffline carrying was_online = 0 does not mean "last online in
1970". It means the server declined to give a time. Aged against now it becomes
~56 years and falls into `older`, inventing a tier that has no members.

`ls -t | xargs python -c "..."` kept failing for reasons that had nothing to do
with the data - wrong directory, mangled heredoc paste - so this is a file in
the repo instead. It finds the artifact itself and says where it looked when it
cannot.

    cd ~/Mkwlsoso && python3 deploy/census_gap_check.py

No venv needed: stdlib only. Nothing is sent and nothing is written.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

#: was_online below this is not a real last-seen time. A genuine Eitaa account
#: cannot have been last seen on 1970-01-02, so anything inside the first day of
#: the epoch is a sentinel the server used to mean "no time for you".
EPOCH_SENTINEL_CUTOFF = 86400

HOUR, DAY = 3600, 86400

#: Where a census artifact can plausibly be. ARTIFACTS_DIR in config.py defaults
#: to the RELATIVE "./artifacts", so the file lands wherever the census was run
#: from - the repo root normally, but $HOME if it was launched from there. Both
#: get searched rather than making the reader guess which happened.
def candidate_dirs() -> list[Path]:
    repo_root = Path(__file__).resolve().parent.parent
    dirs = [
        Path(os.environ["ARTIFACTS_DIR"]) if os.environ.get("ARTIFACTS_DIR") else None,
        repo_root / "artifacts",
        Path.cwd() / "artifacts",
        Path.home() / "artifacts",
        repo_root,
        Path.home(),
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        if d is None:
            continue
        try:
            r = d.resolve()
        except OSError:
            continue
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def find_artifact(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            print(f"  the path you gave is not a file: {p}")
            return None
        return p

    found: list[Path] = []
    print("  looking for a census artifact:")
    for d in candidate_dirs():
        if not d.is_dir():
            print(f"    {d}  (no such directory)")
            continue
        hits = sorted(d.glob("contacts_status_*.json"))
        print(f"    {d}  ->  {len(hits)} match" + ("" if len(hits) == 1 else "es"))
        found.extend(hits)

    if not found:
        print("")
        print("  Nothing found. The census only writes a file when you pass --save:")
        print("    DISPLAY=:99 .venv/bin/python deploy/contacts_status.py \\")
        print("        --account 989991048633 --save")
        print("  It prints the exact path it wrote. You can also pass that path")
        print("  straight to this script as an argument.")
        return None

    newest = max(found, key=lambda p: p.stat().st_mtime)
    if len(found) > 1:
        print(f"  {len(found)} artifacts found; using the newest.")
    return newest


def spread(label: str, values: list[int], now: int) -> None:
    """Print the real age distribution, so a gap can be seen rather than assumed."""
    if not values:
        print(f"  {label}: none")
        return
    bands = [
        (0, HOUR, "< 1h"),
        (HOUR, DAY, "1h - 24h"),
        (DAY, 3 * DAY, "1d - 3d"),
        (3 * DAY, 7 * DAY, "3d - 7d"),
        (7 * DAY, 30 * DAY, "7d - 30d"),
        (30 * DAY, 365 * DAY, "30d - 1y"),
        (365 * DAY, 1 << 62, "> 1y"),
    ]
    print(f"  {label}:")
    for lo, hi, name in bands:
        n = sum(1 for t in values if lo <= max(0, now - t) < hi)
        bar = "#" * min(40, n)
        print(f"    {name:<10} {n:>6,}  {bar}")


def main(argv: list[str]) -> int:
    print("")
    print("=" * 66)
    print("CENSUS GAP CHECK — is `older` real, or is it was_online == 0?")
    print("=" * 66)

    path = find_artifact(argv[1] if len(argv) > 1 else None)
    if path is None:
        return 1
    print(f"  file: {path}")
    print(f"  age : {fmt_since(path.stat().st_mtime)} old")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  cannot read it: {type(exc).__name__}: {exc}")
        return 1

    missing = [k for k in ("server_now", "was_online") if k not in payload]
    if missing:
        print(f"  this file is missing {missing} — it predates the census format")
        print(f"  keys present: {sorted(payload)}")
        return 1

    now = int(payload["server_now"])
    was_online = [int(t) for t in (payload["was_online"] or [])]
    tiers = payload.get("tiers") or {}

    bogus = [t for t in was_online if t < EPOCH_SENTINEL_CUTOFF]
    genuine = [t for t in was_online if t >= EPOCH_SENTINEL_CUTOFF]

    print("")
    print("  WHAT userStatusOffline ACTUALLY CARRIED")
    print("  " + "-" * 62)
    print(f"  contacts with a was_online value : {len(was_online):,}")
    print(f"    sentinel (< 1970-01-02)        : {len(bogus):,}")
    print(f"    genuine timestamps             : {len(genuine):,}")
    if bogus:
        vals = sorted(set(bogus))
        shown = ", ".join(str(v) for v in vals[:8])
        print(f"    distinct sentinel values       : {shown}"
              + (f"  (+{len(vals) - 8} more)" if len(vals) > 8 else ""))

    print("")
    spread("age distribution of the GENUINE timestamps", genuine, now)

    if genuine:
        oldest, newest = min(genuine), max(genuine)
        print("")
        print(f"  genuine oldest : {stamp(oldest)}  ({fmt_since(oldest, now)} ago)")
        print(f"  genuine newest : {stamp(newest)}  ({fmt_since(newest, now)} ago)")

    print("")
    print("  VERDICT")
    print("  " + "-" * 62)
    reported_older = int(tiers.get("older", 0))
    if reported_older:
        print(f"  contacts_status.py reported older = {reported_older:,}")
    if bogus and reported_older == len(bogus):
        print(f"  Every one of those {len(bogus):,} is a was_online == 0 sentinel.")
        print("  There is NO 29-day gap. The `older` tier is entirely artificial:")
        print("  bucket() aged a sentinel against now and got ~56 years.")
        print("  Those contacts belong in `unknown` — the server gave no time.")
    elif bogus:
        print(f"  {len(bogus):,} sentinels are inflating `older` "
              f"(reported {reported_older:,}), but not all of it.")
        print("  Both faults are present: the sentinel bug AND some real old data.")
    else:
        print("  No sentinels. Every was_online is a real timestamp, so the gap")
        print("  is a property of the data, not a bucketing bug.")

    if genuine:
        span_h = (now - min(genuine)) / 3600.0
        print("")
        if span_h <= 48:
            print(f"  Every genuine timestamp is within {span_h:.1f} hours.")
            print("  So Eitaa hands out an exact was_online ONLY for very recent")
            print("  activity and collapses everything older into the hidden")
            print("  categories. These tiers can therefore never be populated,")
            print("  on ANY account, no matter what privacy settings people use:")
            print("      seen_3d   seen_week   seen_month   older")
            print("  The 11-tier send order is really 5 tiers. That is a design")
            print("  fact, not a quirk of this one account.")
        else:
            print(f"  Genuine timestamps span {span_h / 24:.1f} days, so the")
            print("  mid-range tiers are reachable and worth keeping.")
    print("")
    return 0


def stamp(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def fmt_since(ts: float, now: float | None = None) -> str:
    s = int(max(0, (now if now is not None else time.time()) - ts))
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


if __name__ == "__main__":
    sys.exit(main(sys.argv))
