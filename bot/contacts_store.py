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


def path_for(account: str) -> Path:
    return config.DATA_DIR / f"contacts_{account}.json"


def load(account: str) -> dict:
    """Read the cache. Never raises; missing/corrupt -> an empty record."""
    p = path_for(account)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("contacts"), list):
                return data
        except Exception:  # noqa: BLE001 - a corrupt cache must never break a job
            pass
    return {"account": account, "updated": 0, "count": 0, "contacts": []}


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
        # Carried over when a previous save already worked it out, so a
        # re-save from a source without statuses does not wipe the tiers.
        if c.get("tier"):
            entry["tier"] = str(c["tier"])
            if isinstance(c.get("tier_rank"), int):
                entry["tier_rank"] = c["tier_rank"]
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

    # Persist the send-order TIER alongside each contact.
    #
    # The tier has to live here because the browser-free send path has no page to
    # ask: it iterates saved peers only. Computing it once at collection time is
    # what lets BOTH send paths order themselves identically.
    #
    # Only the tier is kept, never the raw status. A status is a moment
    # ("online") and would be a lie an hour later, whereas a tier is a band --
    # "seen within 24h", "Eitaa says last month" -- and stays true for as long as
    # this cache is meant to be used. Tier 1 is the one exception and is treated
    # as such by whoever reads it.
    try:
        from eitaa.send_order import TIER_INDEX, classify
        for src, dst in zip(contacts or [], clean):
            if not isinstance(src, dict) or not src.get("status"):
                continue
            tier, _reason = classify(src.get("status"),
                                     was_online=src.get("was_online"),
                                     expires=src.get("expires"))
            dst["tier"] = tier
            dst["tier_rank"] = TIER_INDEX[tier]
    except Exception as exc:  # noqa: BLE001 - never lose a contacts list over this
        print(f"[contacts] send-order tiers not saved: "
              f"{type(exc).__name__}: {exc}", flush=True)

    record = {"account": account, "updated": time.time(),
              "count": len(clean), "contacts": clean}
    p = path_for(account)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
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


def tiers(account: str) -> dict[str, int]:
    """title -> send-order tier rank, for the contacts that have one.

    Empty when this account's cache predates tier saving, or when the contacts
    came from a source with no status. An empty map means "do not reorder", never
    "everyone is in the bottom tier" -- ordering on absent data would look like it
    had worked while sending to the least active people first.
    """
    out: dict[str, int] = {}
    for c in contacts(account):
        title = str(c.get("title") or "").strip()
        rank = c.get("tier_rank")
        if title and isinstance(rank, int) and title not in out:
            out[title] = rank
    return out


def tier_counts(account: str) -> dict[str, int]:
    """How many saved contacts sit in each tier, by tier NAME."""
    out: dict[str, int] = {}
    for c in contacts(account):
        t = c.get("tier")
        if t:
            out[str(t)] = out.get(str(t), 0) + 1
    return out


def forget(account: str) -> bool:
    """Delete the cache (used when an account is removed)."""
    p = path_for(account)
    try:
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False
