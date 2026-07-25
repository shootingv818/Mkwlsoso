"""Peer store for the browser-free client (isolated, deletable).

A "peer" here is the 20-byte Eitaa inputPeerUser blob (ctor 0xdde8a54c +
user_id:long + access_hash:long) that `messages.sendMessage` / `sendMedia`
need. Without it a browser-free send is impossible, so every peer we ever
learn is persisted per account and reused forever.

This module is the SINGLE owner of `artifacts/sessions/peers_<account>.json`.
The on-disk format is exactly the one `cli.py direct-capture-peer` already
wrote, so existing files keep working unchanged:

    {
      "<label>": {"peer_hex": "...", "user_id": 123, "access_hash": 456},
      "id:123":  {"peer_hex": "...", "user_id": 123, "access_hash": 456}
    }

Entries are stored TWICE on purpose: once under a human label (a contact name,
so `--to "علی"` keeps working) and once under the canonical `id:<user_id>` key,
so a peer_id coming from the contacts list resolves without knowing the name.

Where peers come from (all existing, proven paths):
  1. `contacts.importContacts` — the server answers with the imported users AND
     their access_hash. Harvested while building contacts.
  2. `appPeersManager.getInputPeerById` in the page — resolves already-known
     contacts to a real inputPeer (see eitaa/contacts_bridge.js harvestPeers).
  3. `cli.py direct-capture-peer` — one controlled browser send per contact.

Isolation: nothing outside `direct/` is imported. Deleting `direct/` reverts
the project; the peer files are gitignored artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from config import config

from .eitaa_tl import input_peer_self as _input_peer_user

# The peer blob is a fixed 20 bytes: ctor(4) + user_id(8) + access_hash(8).
PEER_SIZE = 20


def peers_path(account: str) -> Path:
    return config.ARTIFACTS_DIR / "sessions" / f"peers_{account}.json"


def load(account: str) -> dict:
    """Read the account's peer file. Never raises; missing/corrupt -> {}."""
    p = peers_path(account)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - corrupt file must not break a job
        return {}
    return data if isinstance(data, dict) else {}


def _write(account: str, peers: dict) -> None:
    p = peers_path(account)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(peers, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def peer_bytes(user_id: int, access_hash: int) -> bytes:
    """Build the 20-byte inputPeerUser for a user (proven builder in eitaa_tl)."""
    return _input_peer_user(int(user_id), int(access_hash))


def _entry(user_id: int, access_hash: int, peer: bytes | None = None) -> dict:
    if peer is None:
        peer = peer_bytes(user_id, access_hash)
    return {"peer_hex": peer.hex(), "user_id": int(user_id),
            "access_hash": int(access_hash)}


def save_peer(account: str, label: str, peer: bytes, user_id: int,
              access_hash: int) -> None:
    """Persist ONE peer under both its label and its canonical id key.

    Same behaviour as the original cli.py `_save_peer`, plus the id alias.
    """
    peers = load(account)
    entry = _entry(user_id, access_hash, peer)
    if label:
        peers[label] = entry
    peers[f"id:{int(user_id)}"] = entry
    _write(account, peers)


def save_users(account: str, users) -> int:
    """Persist many harvested users at once.

    `users` is an iterable of dicts with at least `user_id` and `access_hash`
    (an optional `label`/`name`/`phone` becomes the human key). Entries with a
    missing or zero access_hash are skipped -- a peer without it is unusable.

    Returns how many NEW peers were added (by id).
    """
    peers = load(account)
    added = 0
    for u in users or []:
        try:
            uid = int(u.get("user_id"))
            ah_raw = u.get("access_hash")
            if ah_raw is None or str(ah_raw) == "":
                continue
            ah = int(ah_raw)
        except (TypeError, ValueError):
            continue
        if not uid or not ah:
            continue
        key = f"id:{uid}"
        if key not in peers:
            added += 1
        entry = _entry(uid, ah)
        peers[key] = entry
        label = u.get("label") or u.get("name") or u.get("phone")
        if label:
            peers[str(label)] = entry
    if added or users:
        _write(account, peers)
    return added


def resolve(account: str, key, peers: dict | None = None) -> bytes | None:
    """Return the 20-byte peer for a label, a user_id, or an `id:N` key."""
    if key is None:
        return None
    peers = load(account) if peers is None else peers
    k = str(key)
    entry = peers.get(k) or peers.get(f"id:{k}")
    if not entry:
        return None
    hexstr = entry.get("peer_hex")
    if hexstr:
        try:
            raw = bytes.fromhex(hexstr)
        except ValueError:
            raw = b""
        if len(raw) == PEER_SIZE:
            return raw
    # Fall back to rebuilding it from the ids (older/partial entries).
    try:
        return peer_bytes(int(entry["user_id"]), int(entry["access_hash"]))
    except (KeyError, TypeError, ValueError):
        return None


def targets(account: str) -> list[tuple[str, bytes]]:
    """Every distinct peer we can send to, as (label, peer_bytes).

    De-duplicated by user_id, and the nicest available label is used: a human
    name wins over the `id:N` alias, so log cards stay readable.
    """
    peers = load(account)
    best: dict[int, tuple[str, bytes]] = {}
    for key, entry in peers.items():
        if not isinstance(entry, dict):
            continue
        raw = resolve(account, key, peers=peers)
        if raw is None:
            continue
        try:
            uid = int(entry.get("user_id"))
        except (TypeError, ValueError):
            continue
        label = str(key)
        current = best.get(uid)
        # Prefer a real name over the "id:<n>" alias.
        if current is None or (current[0].startswith("id:") and not label.startswith("id:")):
            best[uid] = (label, raw)
    return list(best.values())


def count(account: str) -> int:
    """How many distinct sendable peers this account has."""
    return len(targets(account))


def forget(account: str) -> bool:
    """Delete the account's peer file (used when an account is removed)."""
    p = peers_path(account)
    try:
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False
