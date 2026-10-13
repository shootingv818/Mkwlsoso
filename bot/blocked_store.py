"""Recipients Eitaa refuses to accept messages for, per account.

Measured live on a healthy account: sending to 12 saved contacts produced 6
deliveries and 6 PEER_FLOOD refusals, and the split was per RECIPIENT, not
per rate - the same peers were refused sequentially with a 3s gap and
concurrently with a 1s gap. PEER_FLOOD there means Eitaa will not deliver from
this account to that person (no two-way contact / their privacy settings), and it
does not expire on a timer.

Retrying those people on every campaign is pure waste: on that account it was
half of the list, so every run took twice as long as it needed to and produced
hundreds of avoidable errors - which is itself a signal that gets accounts
restricted. This store remembers them so later runs skip them.

One file per account: `DATA_DIR/blocked_<account>.json`

    {
      "account": "989...",
      "updated": 1769300000.0,
      "peers": {"1032988": {"code": "PEER_FLOOD", "hits": 2, "last": 1769300000.0}}
    }

It is a cache, not a verdict: `clear()` (panel: Reset Blocked) wipes it, and a
peer is only added for refusals that are about the RELATIONSHIP (PEER_FLOOD,
USER_PRIVACY_RESTRICTED...), never for a timed FLOOD_WAIT or a transport error.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from config import config

#: Refusals that mean "not deliverable from this account", not "slow down".
_PERMANENT = (
    "PEER_FLOOD",
    "USER_PRIVACY_RESTRICTED",
    "USER_IS_BLOCKED",
    "USER_BANNED_IN_CHANNEL",
    "YOU_BLOCKED_USER",
    "USER_DEACTIVATED",
    "CHAT_WRITE_FORBIDDEN",
)


def is_permanent(code: object) -> bool:
    """Is this refusal about the recipient rather than the rate?"""
    up = str(code or "").upper()
    if re.search(r"FLOOD_WAIT_\d+", up):
        return False  # timed, retry later
    return any(p in up for p in _PERMANENT)


def path_for(account: str) -> Path:
    return config.DATA_DIR / f"blocked_{account}.json"


def load(account: str) -> dict:
    p = path_for(account)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("peers"), dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    return {"account": account, "updated": 0.0, "peers": {}}


def peers(account: str) -> dict:
    return load(account).get("peers") or {}


def count(account: str) -> int:
    return len(peers(account))


class Blocklist:
    """In-memory view for one run; written once at the end."""

    def __init__(self, account: str, data: dict) -> None:
        self.account = account
        self.peers: dict = data.get("peers") or {}
        self.added = 0

    def has(self, peer_id: object) -> bool:
        return bool(peer_id) and str(peer_id) in self.peers

    def add(self, peer_id: object, code: object) -> bool:
        """Record a permanent refusal. Returns True when it was a new entry."""
        if not peer_id or not is_permanent(code):
            return False
        key = str(peer_id)
        entry = self.peers.get(key)
        if entry:
            entry["hits"] = int(entry.get("hits", 1)) + 1
            entry["last"] = time.time()
            return False
        self.peers[key] = {"code": str(code)[:60], "hits": 1, "last": time.time()}
        self.added += 1
        return True

    def flush(self) -> None:
        record = {"account": self.account, "updated": time.time(),
                  "peers": self.peers}
        p = path_for(self.account)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except OSError:
            pass  # a cache write must never break a send


def open_list(account: str) -> Blocklist:
    return Blocklist(account, load(account))


def clear(account: str) -> bool:
    p = path_for(account)
    try:
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False
