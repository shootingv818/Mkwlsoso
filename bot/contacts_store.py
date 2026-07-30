"""Per-account contacts cache.

Collecting an account's contacts means scrolling Eitaa's virtualized list in the
browser, which takes minutes. Doing that at the start of every single send was
the slowest part of the whole bot, so the list is collected ONCE and saved here;
later sends read the file and start delivering immediately.

One file per account: `DATA_DIR/contacts_<account>.json`

    {
      "account": "989304683887",
      "updated": 1769300000.0,
      "count": 350,
      "contacts": [{"title": "...", "peer_id": "123"}, ...]
    }

`peer_id` is kept because it drives the fast in-page send (Eitaa's own engine,
no UI typing); `title` is the search fallback and the label used in log cards.
No phone numbers or message content are stored.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from config import config
from bot import jsoncache


def path_for(account: str) -> Path:
    return config.DATA_DIR / f"contacts_{account}.json"


def load(account: str) -> dict:
    """Read the cache. Never raises; missing/corrupt -> an empty record."""
    empty = lambda: {"account": account, "updated": 0, "count": 0, "contacts": []}
    data = jsoncache.load_json(path_for(account), empty)
    if isinstance(data, dict) and isinstance(data.get("contacts"), list):
        return data
    return empty()


def save(account: str, contacts: list[dict]) -> dict:
    """Persist a freshly collected contacts list (deduped, blanks dropped).

    `access_hash` is kept when the source provides it (the API contacts bridge
    does; the old DOM scrape does not). It is what lets a peer be addressed
    without relying on the browser's in-memory peer cache.
    """
    clean: list[dict] = []
    seen: set[str] = set()
    for c in contacts or []:
        title = str(c.get("title") or "").strip()
        peer_id = c.get("peer_id")
        peer_id = str(peer_id) if peer_id not in (None, "") else None
        if not title and not peer_id:
            continue
        key = peer_id or title
        if key in seen:
            continue
        seen.add(key)
        entry = {"title": title}
        if peer_id:
            entry["peer_id"] = peer_id
        access_hash = c.get("access_hash")
        if access_hash not in (None, ""):
            entry["access_hash"] = str(access_hash)
        clean.append(entry)

    # Guard against a partial collection replacing a complete one. The DOM
    # scroll fallback is capped and returns a TRUNCATED list with no
    # access_hash; letting that overwrite a full API list would silently shrink
    # the reach of every later send (this is how "1,190 of 6,436" happened).
    if clean:
        prev = load(account)
        prev_contacts = prev.get("contacts") or []
        prev_had_hash = any(c.get("access_hash") for c in prev_contacts)
        new_has_hash = any(c.get("access_hash") for c in clean)
        if (prev_had_hash and not new_has_hash
                and len(clean) < len(prev_contacts)):
            print(f"[contacts] keeping the existing {len(prev_contacts)} saved "
                  f"contacts for {account}: the new list has only {len(clean)} "
                  f"and no access_hash (looks truncated)", flush=True)
            return prev

    record = {"account": account, "updated": time.time(),
              "count": len(clean), "contacts": clean}
    jsoncache.write_json(path_for(account), record)
    return record


def contacts(account: str) -> list[dict]:
    return load(account).get("contacts") or []


def count(account: str) -> int:
    return int(load(account).get("count") or 0)


def updated(account: str) -> float:
    return float(load(account).get("updated") or 0)


def age_hours(account: str) -> float | None:
    """How old the cache is, or None when there is none."""
    ts = updated(account)
    if not ts:
        return None
    return max(0.0, (time.time() - ts) / 3600.0)


def items(account: str) -> list[tuple[str, str | None]]:
    """(title, peer_id) pairs — the exact shape the send loop iterates."""
    return [(c.get("title", ""), c.get("peer_id")) for c in contacts(account)
            if c.get("title") or c.get("peer_id")]


def forget(account: str) -> bool:
    """Delete the cache (used when an account is removed)."""
    p = path_for(account)
    try:
        if p.is_file():
            p.unlink()
            jsoncache.invalidate(p)
            return True
    except OSError:
        pass
    return False
