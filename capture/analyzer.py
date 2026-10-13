"""Analyzer.

Reads a capture run's events.jsonl, separates action-phase traffic from the
idle baseline noise, groups it by endpoint / socket, and writes a concise
report.md describing the most likely request/response(s) behind the action.

This is intentionally heuristic: it surfaces candidates for a human to confirm,
not an automatic protocol decoder.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import config


def _load_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def _endpoint(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.netloc}{p.path}"
    except Exception:  # noqa: BLE001
        return url


def analyze(run_id: str) -> Path:
    run_dir = config.ARTIFACTS_DIR / run_id
    events = _load_events(run_dir)

    baseline_endpoints: set[str] = set()
    action_http: list[dict[str, Any]] = []
    action_ws_frames: list[dict[str, Any]] = []
    ws_open: list[dict[str, Any]] = []

    for e in events:
        phase = e.get("phase")
        source = e.get("source")
        if source == "http" and e.get("kind") == "request":
            ep = _endpoint(e.get("url", ""))
            if phase == "baseline":
                baseline_endpoints.add(ep)
            elif phase in {"action", "trail"}:
                action_http.append(e)
        elif source == "ws":
            if e.get("kind") == "ws_open":
                ws_open.append(e)
            elif e.get("kind") == "ws_frame" and phase in {"action", "trail"}:
                action_ws_frames.append(e)

    # HTTP requests during the action that were NOT seen while idle: the most
    # interesting candidates for the operation's real endpoint.
    novel_http = [e for e in action_http if _endpoint(e.get("url", "")) not in baseline_endpoints]
    novel_by_ep: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in novel_http:
        novel_by_ep[_endpoint(e.get("url", ""))].append(e)

    ws_dir_counts = Counter(f.get("direction") for f in action_ws_frames)

    report = _render_report(
        run_id=run_id,
        run_dir=run_dir,
        total_events=len(events),
        baseline_endpoint_count=len(baseline_endpoints),
        novel_by_ep=novel_by_ep,
        action_http_count=len(action_http),
        ws_open=ws_open,
        action_ws_frames=action_ws_frames,
        ws_dir_counts=ws_dir_counts,
    )
    out = run_dir / "report.md"
    out.write_text(report, encoding="utf-8")
    return out


def _render_report(
    *,
    run_id: str,
    run_dir: Path,
    total_events: int,
    baseline_endpoint_count: int,
    novel_by_ep: dict[str, list[dict[str, Any]]],
    action_http_count: int,
    ws_open: list[dict[str, Any]],
    action_ws_frames: list[dict[str, Any]],
    ws_dir_counts: Counter,
) -> str:
    lines: list[str] = []
    lines.append(f"# Capture report - {run_id}")
    lines.append("")
    lines.append(f"- total events: {total_events}")
    lines.append(f"- baseline endpoints (idle noise): {baseline_endpoint_count}")
    lines.append(f"- action-phase HTTP requests: {action_http_count}")
    lines.append(f"- websocket connections observed: {len(ws_open)}")
    lines.append(
        f"- action-phase ws frames: {len(action_ws_frames)} "
        f"(sent={ws_dir_counts.get('sent', 0)}, received={ws_dir_counts.get('received', 0)})"
    )
    lines.append("")

    lines.append("## Candidate HTTP endpoints (new during the action)")
    lines.append("")
    if not novel_by_ep:
        lines.append("_No new HTTP endpoints appeared during the action._")
        lines.append("If the operation is real-time, inspect the WebSocket frames below.")
    else:
        for ep, evs in sorted(novel_by_ep.items(), key=lambda kv: -len(kv[1])):
            methods = sorted({e.get("method", "?") for e in evs})
            sample = evs[0]
            body = sample.get("body", {})
            lines.append(f"### `{ep}`")
            lines.append(f"- methods: {', '.join(methods)}")
            lines.append(f"- occurrences: {len(evs)}")
            lines.append(f"- body kind: {body.get('kind', 'n/a')}, size: {body.get('size', 0)}")
            json_shape = body.get("json")
            if isinstance(json_shape, dict):
                keys = list(json_shape.keys())[:20]
                lines.append(f"- json keys: {', '.join(keys) if keys else '(none)'}")
            lines.append("")

    lines.append("## WebSocket summary")
    lines.append("")
    if not ws_open:
        lines.append("_No WebSocket connections were observed._")
    else:
        for w in ws_open:
            lines.append(f"- connection #{w.get('conn_id')}: {w.get('url')}")
        lines.append("")
        # Show a few distinct frame shapes from the action window.
        shapes: dict[str, dict[str, Any]] = {}
        for f in action_ws_frames:
            body = f.get("body", {})
            key = f"{f.get('direction')}|{f.get('opcode')}|{body.get('kind')}|{body.get('size')}"
            if key not in shapes:
                shapes[key] = f
        lines.append("### Distinct action-phase frame shapes (first seen)")
        for key, f in list(shapes.items())[:20]:
            body = f.get("body", {})
            extra = ""
            json_shape = body.get("json")
            if isinstance(json_shape, dict):
                extra = " keys=" + ",".join(list(json_shape.keys())[:12])
            lines.append(
                f"- {f.get('direction')} {f.get('opcode')} "
                f"kind={body.get('kind')} size={body.get('size')}{extra}"
            )
    lines.append("")

    lines.append("## Next step")
    lines.append("")
    lines.append("Confirm which candidate above corresponds to the action, then decide:")
    lines.append("- **Browser-driver**: keep clicking via Playwright (safest).")
    lines.append("- **Hybrid**: replay the confirmed request from the live browser session.")
    lines.append("")
    lines.append(f"Artifacts: `{run_dir}` (events.jsonl, http.jsonl, ws.jsonl, screenshots, trace.zip)")
    lines.append("")
    return "\n".join(lines)
