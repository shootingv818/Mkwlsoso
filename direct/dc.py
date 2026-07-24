"""Data-center configuration for the direct client.

Eitaa Web talks MTProto over HTTPS to its shard hosts. The exact dc_id ->
host/URL mapping is CONFIRMED with `cli.py direct-capture-transport` (which
records the real request URL per DC); until then these are best-effort
defaults from the earlier capture and are overridable via env.

api_id/api_hash are Eitaa Web's own public client credentials (extracted in
artifacts/params.json; also settable via EITAA_API_ID/EITAA_API_HASH). They
are NOT the user's secret.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

LAYER = 135

# CONFIRMED from the transport capture: Eitaa POSTs to https://<shard>.eitaa.com/eitaa/
# (path "/eitaa/"; hadi served media). The dc_id -> shard mapping is not known yet,
# so the probe TRIES all known shards with the home-DC auth_key and keeps the one
# that returns a decryptable MTProto reply.
DEFAULT_PATH = "/eitaa/"
EITAA_SHARD_HOSTS = ["majid", "ghasem", "hossein", "vahid", "hadi"]


def candidate_urls(dc_id: int) -> list[str]:
    """All URLs worth trying for this DC (env override wins)."""
    override = _load_env_hosts().get(dc_id)
    if override:
        return [override]
    return [f"https://{h}.eitaa.com{DEFAULT_PATH}" for h in EITAA_SHARD_HOSTS]


def _load_env_hosts() -> dict:
    """MKWL_DC_HOSTS='2=https://x.eitaa.com/apiw1,4=https://y...' overrides."""
    raw = os.environ.get("MKWL_DC_HOSTS", "").strip()
    out: dict = {}
    if not raw:
        return out
    for part in raw.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            try:
                out[int(k.strip())] = v.strip()
            except ValueError:
                continue
    return out


def dc_url(dc_id: int) -> str:
    """Return the primary URL to POST MTProto payloads to for this DC."""
    return candidate_urls(dc_id)[0]


def resolve_api_creds() -> tuple[int, str]:
    """api_id/api_hash from env, else any captured artifacts params.json."""
    aid = (os.environ.get("EITAA_API_ID") or "").strip()
    ah = (os.environ.get("EITAA_API_HASH") or "").strip()
    if aid.isdigit() and ah:
        return int(aid), ah
    try:
        base = os.environ.get("ARTIFACTS_DIR", "./artifacts")
        for p in glob.glob(str(Path(base) / "**" / "params.json"), recursive=True):
            try:
                d = json.loads(Path(p).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            cid = d.get("api_id") or d.get("apiId")
            ch = d.get("api_hash") or d.get("apiHash")
            if cid and ch:
                try:
                    return int(cid), str(ch)
                except (TypeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001
        pass
    return 0, ""
