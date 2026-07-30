"""Per-account send ledger: who already received the CURRENT content.

Why this exists: a send job used to keep no record of its progress. When a run
stopped early -- and one live run stopped at recipient 300 of 1,099 because of a
transient failure streak -- the next run started again from the top, so the first
300 contacts received the SAME message twice while the remaining 799 still never
got it.

The ledger is keyed by a fingerprint of the content being sent, so:
  * re-running the same content resumes where it stopped, skipping delivered ones
  * changing the text/file/caption starts a fresh ledger automatically
  * nothing is skipped silently for a different message

One file per account: `DATA_DIR/progress_<account>.json`

    {
      "account": "989153222956",
      "key": "file:9f2a...",
      "updated": 1769300000.0,
      "done": ["12345", "title:Ali"]
    }

Targets are identified by peer_id when there is one, else "title:<name>".
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from config import config
from bot import jsoncache


def path_for(account: str) -> Path:
    return config.DATA_DIR / f"progress_{account}.json"


def content_key(content: dict) -> str:
    """A stable fingerprint of what is being sent."""
    kind = str((content or {}).get("kind") or "none")
    if kind == "file":
        raw = "|".join([
            kind,
            str(content.get("file_path") or ""),
            str(content.get("file_name") or ""),
            str(content.get("caption") or ""),
        ])
    else:
        raw = "|".join([kind, str(content.get("text") or "")])
    return kind + ":" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def target_key(title: str, peer_id: str | None) -> str:
    return str(peer_id) if peer_id else "title:" + str(title or "")


class Ledger:
    """Delivered targets for one account + one content fingerprint.

    `done` holds one entry per PERSON (so counting it is meaningful). `aliases`
    holds the secondary identity of the same person -- their display name -- and
    is only used for lookups, never counted.
    """

    def __init__(self, account: str, key: str, done: set[str],
                 aliases: set[str] | None = None) -> None:
        self.account = account
        self.key = key
        self.done: set[str] = done
        self.aliases: set[str] = aliases or set()
        self._dirty = 0

    def has(self, title: str, peer_id: str | None) -> bool:
        """Was this target already delivered to?

        BOTH identities are checked, because the same person can arrive with a
        peer_id from the API list and without one from the DOM scrape. Matching
        on one key only would resend to everyone whose id changed source.
        """
        key = target_key(title, peer_id)
        if key in self.done or key in self.aliases:
            return True
        if title:
            name_key = "title:" + str(title)
            if name_key in self.done or name_key in self.aliases:
                return True
        return False

    def mark(self, title: str, peer_id: str | None, flush_every: int = 25) -> None:
        self.done.add(target_key(title, peer_id))
        # Remember the name as an alias, so a later run that lost the peer_id
        # (or gained one) still recognises this person as already served.
        if peer_id and title:
            self.aliases.add("title:" + str(title))
        self._dirty += 1
        # Batch the writes: one fsync per recipient would be pure overhead, and
        # losing at most `flush_every` entries only costs a few duplicates.
        if self._dirty >= flush_every:
            self.flush()

    def flush(self) -> None:
        self._dirty = 0
        record = {"account": self.account, "key": self.key,
                  "updated": time.time(), "done": sorted(self.done),
                  "aliases": sorted(self.aliases)}
        try:
            jsoncache.write_json(path_for(self.account), record)
        except OSError:
            pass  # a ledger write must never break a running send


def open_ledger(account: str, key: str) -> Ledger:
    """Load the ledger for this content, or start a fresh one."""
    p = path_for(account)
    if p.is_file():
        try:
            data = jsoncache.load_json(p, dict)
            if isinstance(data, dict) and data.get("key") == key:
                done = {str(x) for x in (data.get("done") or [])}
                aliases = {str(x) for x in (data.get("aliases") or [])}
                return Ledger(account, key, done, aliases)
        except Exception:  # noqa: BLE001 - corrupt ledger -> start clean
            pass
    return Ledger(account, key, set())


def clear(account: str) -> bool:
    """Forget an account's ledger (used to force a full resend)."""
    p = path_for(account)
    try:
        if p.is_file():
            p.unlink()
            jsoncache.invalidate(p)
            return True
    except OSError:
        pass
    return False


def done_count(account: str, key: str) -> int:
    p = path_for(account)
    if not p.is_file():
        return 0
    try:
        data = jsoncache.load_json(p, dict)
        if isinstance(data, dict) and data.get("key") == key:
            return len(data.get("done") or [])
    except Exception:  # noqa: BLE001
        pass
    return 0
