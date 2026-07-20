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

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from config import config

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

# DC option-ish objects mentioning an eitaa host near an id/port.
RE_DC_CTX = re.compile(r".{0,80}[a-z0-9\-]+\.eitaa\.com.{0,80}", re.I)


def _js_files(assets_dir: Path) -> list[Path]:
    return sorted(
        [p for p in assets_dir.glob("*") if p.suffix.lower() in {".js", ".mjs"} and p.is_file()]
    )


def extract_from_text(text: str, out: dict[str, Any]) -> None:
    for m in RE_HOST.finditer(text):
        out["_hosts"][m.group(1).lower()] += 1

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
    out.pop("_hexset", None)

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
        "api_candidates": out["api_candidates"],
        "layer_candidates": dict(layers.most_common(10)),
        "rsa_pem_count": len(out["rsa_pem"]),
        "rsa_pem": out["rsa_pem"],
        "rsa_hex_modulus_candidates": out["rsa_hex_modulus_candidates"][:12],
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
    L.append("## RSA public keys")
    L.append(f"- PEM blocks: {data['rsa_pem_count']}")
    L.append(f"- hex modulus candidates: {len(data['rsa_hex_modulus_candidates'])}")
    L.append(f"- exponent '010001' occurrences: {data['exponent_010001_count']}")
    if data["rsa_hex_modulus_candidates"]:
        first = data["rsa_hex_modulus_candidates"][0]
        L.append(f"- first modulus candidate: {first['len']} hex chars "
                 f"({first['len'] * 4} bits) -> {first['hex'][:48]}...")
    L.append("")
    L.append(f"Full details written to: {params_path}")
    return "\n".join(L)
