"""Protocol dossier builder.

Reads a probe run's events.jsonl (which includes both the HTTP recorder events
and the in-page hook records) and produces protocol_dossier.md: a focused
summary of Eitaa's transport, cryptography, worker/wasm involvement, and the
datacenter endpoints, plus a first read on MTProto/Telethon compatibility.

Heuristic and evidence-first: it reports what was observed and flags unknowns,
it does not claim a protocol it did not see.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import config

# Eitaa datacenter-style endpoints seen in earlier captures.
DC_HINT = "/eitaa/"


def _load(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:  # noqa: BLE001
        return ""


def build_dossier(run_id: str) -> Path:
    run_dir = config.ARTIFACTS_DIR / run_id
    events = _load(run_dir)

    hooks = [e for e in events if e.get("source") == "hook"]
    http = [e for e in events if e.get("source") == "http"]

    # --- crypto.subtle usage ---
    subtle = Counter()
    subtle_algs = Counter()
    for e in hooks:
        k = e.get("kind", "")
        if k.startswith("subtle_"):
            subtle[k] += 1
            subtle_algs[e.get("alg", "unknown")] += 1

    # --- wasm ---
    wasm_ready = [e for e in hooks if e.get("kind") == "wasm_ready"]
    wasm_exports: list[str] = []
    for e in wasm_ready:
        for ex in e.get("exports", []) or []:
            if ex not in wasm_exports:
                wasm_exports.append(ex)
    wasm_instantiations = sum(1 for e in hooks if e.get("kind") in ("wasm_instantiate", "wasm_instantiate_streaming"))

    # --- worker boundary ---
    worker_new = [e for e in hooks if e.get("kind") == "worker_new"]
    worker_posts = [e for e in hooks if e.get("kind") == "worker_post"]
    worker_msgs = [e for e in hooks if e.get("kind") == "worker_msg"]

    # --- datacenter frames (raw bytes captured by hooks) ---
    dc_frames = []
    for e in hooks:
        if e.get("kind") in ("fetch_req", "fetch_resp", "xhr_req", "xhr_resp"):
            url = e.get("url", "")
            if DC_HINT in url:
                dc_frames.append(e)
    dc_hosts = Counter(_host(e.get("url", "")) for e in dc_frames)
    frames_with_raw = sum(1 for e in dc_frames if e.get("reqB64") or e.get("respB64"))

    # --- websocket ---
    ws_new = [e for e in hooks if e.get("kind") == "ws_new"]

    # --- http hosts overall ---
    http_hosts = Counter(_host(e.get("url", "")) for e in http if e.get("kind") == "request")

    report = _render(
        run_id=run_id,
        run_dir=run_dir,
        n_events=len(events),
        n_hooks=len(hooks),
        subtle=subtle,
        subtle_algs=subtle_algs,
        wasm_exports=wasm_exports,
        wasm_instantiations=wasm_instantiations,
        worker_new=worker_new,
        worker_posts=worker_posts,
        worker_msgs=worker_msgs,
        dc_frames=dc_frames,
        dc_hosts=dc_hosts,
        frames_with_raw=frames_with_raw,
        ws_new=ws_new,
        http_hosts=http_hosts,
    )
    out = run_dir / "protocol_dossier.md"
    out.write_text(report, encoding="utf-8")
    return out


def _sizes(frames: list[dict[str, Any]], key: str) -> list[int]:
    out = []
    for f in frames:
        v = f.get(key)
        if isinstance(v, int):
            out.append(v)
    return out


def _render(**k: Any) -> str:
    L: list[str] = []
    L.append(f"# Eitaa protocol dossier - {k['run_id']}")
    L.append("")
    L.append(f"- total events: {k['n_events']} (hook records: {k['n_hooks']})")
    L.append("")

    # Transport
    L.append("## Transport")
    L.append("")
    dc_hosts = k["dc_hosts"]
    if dc_hosts:
        L.append("Datacenter-style endpoints (`/eitaa/`) hit during the run:")
        for host, n in dc_hosts.most_common():
            L.append(f"- `{host}` x{n}")
    else:
        L.append("_No `/eitaa/` datacenter frames observed in this run._")
    if k["ws_new"]:
        L.append("")
        L.append(f"WebSocket connections opened: {len(k['ws_new'])}")
        for w in k["ws_new"][:5]:
            L.append(f"- {w.get('url')}")
    L.append("")
    req_sizes = _sizes(k["dc_frames"], "reqSize")
    resp_sizes = _sizes(k["dc_frames"], "respSize")
    L.append(f"- datacenter frames captured: {len(k['dc_frames'])} "
             f"(with raw bytes: {k['frames_with_raw']})")
    if req_sizes:
        L.append(f"- request body sizes: min={min(req_sizes)} max={max(req_sizes)} n={len(req_sizes)}")
    if resp_sizes:
        L.append(f"- response body sizes: min={min(resp_sizes)} max={max(resp_sizes)} n={len(resp_sizes)}")
    L.append("")

    # Cryptography
    L.append("## Cryptography (crypto.subtle)")
    L.append("")
    if k["subtle"]:
        L.append("Observed WebCrypto operations:")
        for op, n in k["subtle"].most_common():
            L.append(f"- `{op}` x{n}")
        L.append("")
        L.append("Algorithms:")
        for alg, n in k["subtle_algs"].most_common():
            L.append(f"- `{alg}` x{n}")
        L.append("")
        L.append("_If encryption is visible here, the wire crypto is done in JS via WebCrypto._")
    else:
        L.append("_No crypto.subtle calls were captured._")
        L.append("This strongly suggests the encryption happens inside WASM/worker code")
        L.append("(see WASM/Worker sections), which is the typical MTProto-in-browser pattern.")
    L.append("")

    # WASM
    L.append("## WebAssembly")
    L.append("")
    L.append(f"- instantiations: {k['wasm_instantiations']}")
    if k["wasm_exports"]:
        L.append(f"- exports observed ({len(k['wasm_exports'])}):")
        L.append("  " + ", ".join(k["wasm_exports"][:60]))
        L.append("")
        L.append("_Check these export names for aes/ige/sha/pq/factorize/serialize hints._")
    else:
        L.append("- no wasm exports captured")
    L.append("")

    # Worker boundary
    L.append("## Worker boundary")
    L.append("")
    L.append(f"- workers created: {len(k['worker_new'])}")
    for w in k["worker_new"][:8]:
        L.append(f"  - {w.get('url')}")
    posts = k["worker_posts"]
    msgs = k["worker_msgs"]
    L.append(f"- messages to worker: {len(posts)} | from worker: {len(msgs)}")
    L.append("_Compare 'to worker' (likely plaintext/TL) vs 'from worker' (likely encrypted) sizes"
             " to locate where serialization/encryption happens._")
    L.append("")

    # Compatibility read
    L.append("## MTProto / Telethon compatibility (first read)")
    L.append("")
    signals = []
    if dc_hosts:
        signals.append("multiple named datacenters (MTProto-like)")
    if req_sizes and max(req_sizes) < 1024:
        signals.append("small binary frames (consistent with MTProto messages)")
    if not k["subtle"]:
        signals.append("no WebCrypto -> crypto likely in WASM (Telegram tweb pattern)")
    if k["wasm_instantiations"]:
        signals.append("WASM in use (tweb uses wasm for crypto/opus)")
    if signals:
        for s in signals:
            L.append(f"- signal: {s}")
    else:
        L.append("- not enough signal yet; capture more operations (login, send).")
    L.append("")
    L.append("### Still needed before writing a direct client")
    L.append("- confirm the auth handshake (req_pq / DH) in the login capture")
    L.append("- extract DC addresses + server RSA public keys + api_id/layer from the JS assets")
    L.append("- decide route A (adapt Telethon) / B (custom schema) / C (custom core) / D (wasm bridge)")
    L.append("")
    L.append(f"Artifacts: `{k['run_dir']}` (events.jsonl, hook.jsonl, assets/, storage.json)")
    L.append("")
    return "\n".join(L)
