"""Offline MTProto-parameter extractor.

Scans the JS assets downloaded by `probe` and mines the constants needed to
talk to Eitaa's MTProto servers directly:

- datacenter hostnames (*.eitaa.com)
- api_id / api_hash (tweb App config)
- protocol layer number
- server RSA public keys (PEM or hex modulus + exponent)

These are PUBLIC client constants embedded in every web client, not personal
secrets. Output is written to <run>/params.json for review.

Runs fully offline: no browser, no network. Eitaa Web is Telegram Web K (tweb),
so these patterns follow tweb's structure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from config import config


# --- RSA helpers (stdlib only) ------------------------------------------
def _tl_bytes(b: bytes) -> bytes:
    """Telegram TL 'bytes' serialization (length-prefixed, padded to 4)."""
    n = len(b)
    if n < 254:
        out = bytes([n]) + b
    else:
        out = bytes([254, n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF]) + b
    while len(out) % 4:
        out += b"\x00"
    return out


def _hex_to_bytes(h: str) -> bytes:
    h = h.strip()
    if len(h) % 2:
        h = "0" + h
    return bytes.fromhex(h)


def rsa_fingerprint(mod_hex: str, exp_hex: str = "010001") -> str:
    """MTProto key fingerprint = low 64 bits of SHA1(tl_bytes(n)+tl_bytes(e)).

    Returned as the little-endian int hex the server sends in res_pq.
    """
    n = _hex_to_bytes(mod_hex)
    e = _hex_to_bytes(exp_hex)
    digest = hashlib.sha1(_tl_bytes(n) + _tl_bytes(e)).digest()
    return digest[-8:].hex()  # last 8 bytes (server sends these as the fp)


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = b""
    while n:
        out = bytes([n & 0xFF]) + out
        n >>= 8
    return bytes([0x80 | len(out)]) + out


def _der_int(raw: bytes) -> bytes:
    raw = raw.lstrip(b"\x00") or b"\x00"
    if raw[0] & 0x80:  # keep it positive
        raw = b"\x00" + raw
    return b"\x02" + _der_len(len(raw)) + raw


def rsa_pem(mod_hex: str, exp_hex: str = "010001") -> str:
    """PKCS#1 RSAPublicKey PEM from hex modulus + exponent (stdlib DER)."""
    n = _der_int(_hex_to_bytes(mod_hex))
    e = _der_int(_hex_to_bytes(exp_hex))
    seq = b"\x30" + _der_len(len(n) + len(e)) + n + e
    body = base64.encodebytes(seq).decode().strip()
    return "-----BEGIN RSA PUBLIC KEY-----\n" + body + "\n-----END RSA PUBLIC KEY-----"

# --- regexes -------------------------------------------------------------
RE_HOST = re.compile(r"\b([a-z0-9][a-z0-9\-]*\.eitaa\.com)\b", re.I)

# tweb App config: {id: 2496, hash: "..."} (minified spacing varies)
RE_API = re.compile(r"""id\s*:\s*(\d{3,9})\s*,\s*hash\s*:\s*['"]([0-9a-fA-F]{32})['"]""")
RE_API_REV = re.compile(r"""hash\s*:\s*['"]([0-9a-fA-F]{32})['"]\s*,\s*id\s*:\s*(\d{3,9})""")

# layer: many shapes; collect standalone candidates.
RE_LAYER = re.compile(r"""layer['"\s:=]{1,4}(\d{2,4})""", re.I)
RE_INVOKE_LAYER = re.compile(r"invokeWithLayer[^0-9]{0,6}(\d{2,4})")

# RSA PEM blocks.
RE_PEM = re.compile(r"-----BEGIN[ A-Z]*PUBLIC KEY-----[\s\S]+?-----END[ A-Z]*PUBLIC KEY-----")

# RSA hex: exponent 010001 (65537) is the giveaway; modulus is a long hex run.
RE_EXP = re.compile(r"['\"]010001['\"]")
RE_HEXRUN = re.compile(r"[0-9a-fA-F]{200,}")

# DC id <-> host mapping heuristics (both orders seen in minified tweb).
RE_DC_ID_HOST = re.compile(r"""(\d{1,2})\s*[:=]\s*['"]([a-z0-9\-]+)\.eitaa\.com['"]""", re.I)
RE_DC_HOST_ID = re.compile(r"""['"]([a-z0-9\-]+)\.eitaa\.com['"]\s*[,:]\s*(\d{1,2})\b""", re.I)


def _js_files(assets_dir: Path) -> list[Path]:
    return sorted(
        [p for p in assets_dir.glob("*") if p.suffix.lower() in {".js", ".mjs"} and p.is_file()]
    )


def extract_from_text(text: str, out: dict[str, Any]) -> None:
    for m in RE_HOST.finditer(text):
        out["_hosts"][m.group(1).lower()] += 1

    for m in RE_DC_ID_HOST.finditer(text):
        out["_dc_map"][f"{m.group(1)}:{m.group(2).lower()}"] += 1
    for m in RE_DC_HOST_ID.finditer(text):
        out["_dc_map"][f"{m.group(2)}:{m.group(1).lower()}"] += 1

    for m in RE_API.finditer(text):
        out["api_candidates"].append({"id": int(m.group(1)), "hash": m.group(2)})
    for m in RE_API_REV.finditer(text):
        out["api_candidates"].append({"id": int(m.group(2)), "hash": m.group(1)})

    for m in RE_LAYER.finditer(text):
        out["_layers"][m.group(1)] += 1
    for m in RE_INVOKE_LAYER.finditer(text):
        out["_layers"][m.group(1)] += 5  # stronger signal

    for m in RE_PEM.finditer(text):
        block = m.group(0)
        if block not in out["rsa_pem"]:
            out["rsa_pem"].append(block)

    out["exponent_010001_count"] += len(RE_EXP.findall(text))

    for m in RE_HEXRUN.finditer(text):
        h = m.group(0)
        # RSA-2048 modulus is 512 hex chars; keep 256..1200 to catch 1024/2048/4096.
        if 256 <= len(h) <= 1200 and h.lower() not in out["_hexset"]:
            out["_hexset"].add(h.lower())
            out["rsa_hex_modulus_candidates"].append({"len": len(h), "hex": h})


def extract_run(run_id: str) -> Path:
    run_dir = config.ARTIFACTS_DIR / run_id
    assets_dir = run_dir / "assets"
    if not assets_dir.is_dir():
        raise FileNotFoundError(f"no assets dir at {assets_dir} (run `probe` first)")

    out: dict[str, Any] = {
        "run_id": run_id,
        "_hosts": Counter(),
        "_dc_map": Counter(),
        "api_candidates": [],
        "_layers": Counter(),
        "rsa_pem": [],
        "rsa_hex_modulus_candidates": [],
        "exponent_010001_count": 0,
        "_hexset": set(),
        "scanned_files": [],
    }

    for path in _js_files(assets_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        out["scanned_files"].append(path.name)
        extract_from_text(text, out)

    # Finalize / dedup.
    hosts = out.pop("_hosts")
    layers = out.pop("_layers")
    dc_map = out.pop("_dc_map")
    out.pop("_hexset", None)

    # Turn each hex modulus candidate into a ready-to-use RSA key: PEM + the
    # MTProto fingerprint the server advertises in res_pq.
    rsa_keys = []
    for cand in out["rsa_hex_modulus_candidates"]:
        h = cand["hex"]
        try:
            rsa_keys.append({
                "bits": cand["len"] * 4,
                "fingerprint": rsa_fingerprint(h),
                "pem": rsa_pem(h),
                "modulus_hex": h,
            })
        except Exception:  # noqa: BLE001
            continue

    # Unique api candidates.
    seen = set()
    uniq_api = []
    for c in out["api_candidates"]:
        key = (c["id"], c["hash"])
        if key not in seen:
            seen.add(key)
            uniq_api.append(c)
    out["api_candidates"] = uniq_api

    result = {
        "run_id": run_id,
        "scanned_files": out["scanned_files"],
        "datacenter_hosts": dict(hosts.most_common()),
        "dc_id_host_map": dict(dc_map.most_common()),
        "api_candidates": out["api_candidates"],
        "layer_candidates": dict(layers.most_common(10)),
        "rsa_keys": rsa_keys,
        "rsa_pem_blocks_found": out["rsa_pem"],
        "exponent_010001_count": out["exponent_010001_count"],
    }

    out_path = run_dir / "params.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def summarize(params_path: Path) -> str:
    data = json.loads(params_path.read_text(encoding="utf-8"))
    L: list[str] = []
    L.append(f"# Extracted MTProto params - {data['run_id']}")
    L.append("")
    L.append(f"scanned JS files: {len(data['scanned_files'])}")
    L.append("")
    L.append("## Datacenter hosts")
    for host, n in data["datacenter_hosts"].items():
        L.append(f"- {host} (x{n})")
    L.append("")
    dc_map = data.get("dc_id_host_map", {})
    if dc_map:
        L.append("## DC id -> host candidates")
        for pair, n in dc_map.items():
            L.append(f"- dc{pair} (x{n})")
        L.append("")
    L.append("## api_id / api_hash candidates")
    if data["api_candidates"]:
        for c in data["api_candidates"]:
            L.append(f"- id={c['id']} hash={c['hash']}")
    else:
        L.append("- none found (may be split/obfuscated; check main bundle manually)")
    L.append("")
    L.append("## layer candidates (count)")
    for lv, n in data["layer_candidates"].items():
        L.append(f"- layer {lv} (x{n})")
    L.append("")
    L.append("## RSA public keys (ready for the auth handshake)")
    keys = data.get("rsa_keys", [])
    L.append(f"- keys derived: {len(keys)} | inline PEM blocks: {len(data.get('rsa_pem_blocks_found', []))}")
    L.append(f"- exponent '010001' occurrences: {data['exponent_010001_count']}")
    for i, key in enumerate(keys, 1):
        L.append(f"- key #{i}: {key['bits']} bits, fingerprint={key['fingerprint']}")
    L.append("")
    L.append("_Full PEM keys + fingerprints + DC map are in params.json._")
    L.append("")
    L.append(f"Full details written to: {params_path}")
    return "\n".join(L)
