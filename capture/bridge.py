"""Bridge-discovery analysis.

Companion to capture/bridge.js. It performs a controlled, safe send (the run's
unique marker text -> the owner's own Saved Messages), then inspects the
structural records drained from the in-page instrumentation to answer ONE
question:

    Does the message TEXT (our marker) cross a JS thread boundary
    (Worker / SharedWorker / MessagePort / BroadcastChannel) in PLAINTEXT
    before it is encrypted?

If yes, a direct in-browser "bridge" is feasible: we can hand a high-level
send task to Eitaa's own worker instead of driving the UI. If no, the send is
already encoded/encrypted at the JS boundary and we build the fast UI engine
instead.

The marker is detected either as a plaintext string node OR as marker bytes
inside a small (unencrypted) buffer that was posted across the boundary.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

BRIDGE_JS = Path(__file__).with_name("bridge.js")

# Channels that represent a real cross-thread boundary (where encryption would
# typically live on the far side).
_BOUNDARY_KINDS = {
    "worker_post", "worker_msg",
    "swport_post", "swport_msg",
    "port_post", "port_msg",
    "bc_post", "bc_msg",
}


async def send_marker_to_saved(driver: Any, marker: str) -> str:
    """Send exactly one text message (the marker) to the owner's Saved Messages.

    This is the controlled action whose text we then look for at the JS worker
    boundary. Saved Messages is the owner's own storage, so it bothers nobody.
    """
    from eitaa import selectors as S
    from eitaa.driver import _first_visible

    try:
        await driver.open_saved_messages()
    except Exception as exc:  # noqa: BLE001
        return f"could not open Saved Messages: {exc}"

    box = await _first_visible(driver.page, S.MESSAGE_INPUT, timeout=8000)
    if box is None:
        return "composer not found in Saved Messages"

    try:
        await box.click(timeout=5000)
    except Exception:  # noqa: BLE001
        try:
            await box.evaluate("el => el.focus()")
        except Exception:  # noqa: BLE001
            pass

    await box.type(marker, delay=5)
    await driver.page.wait_for_timeout(150)

    sent = False
    btn = await _first_visible(driver.page, S.SEND_BUTTON, timeout=2500)
    if btn is not None:
        try:
            await btn.click(timeout=4000)
            sent = True
        except Exception:  # noqa: BLE001
            pass
    if not sent:
        try:
            await box.press("Enter")
            sent = True
        except Exception:  # noqa: BLE001
            pass
    await driver.page.wait_for_timeout(500)
    return "sent" if sent else "could not trigger send"


def _bytes_contain_marker(b64: str | None, marker: str) -> bool:
    """True if the decoded buffer contains the marker in a common text encoding.

    A hit here means the payload crossing the boundary was serialized but NOT
    encrypted (the plaintext marker survives), which is just as good as a
    string hit for bridge feasibility.
    """
    if not b64:
        return False
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return False
    for enc in ("utf-8", "utf-16-le", "utf-16-be", "latin-1"):
        try:
            if marker.encode(enc) in raw:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _walk(shape: Any, marker: str, path: str, hits: list[dict], strings: list[str]) -> bool:
    """Recurse a serialized shape. Collect marker hits and short string values.

    Returns True if the marker was found anywhere in this subtree.
    """
    if not isinstance(shape, dict):
        return False
    t = shape.get("t")
    found = False

    if t == "str":
        s = shape.get("s")
        if isinstance(s, str) and s:
            strings.append(s)
        if shape.get("marker"):
            hits.append({"path": path, "value": shape.get("s"), "where": "string"})
            found = True
    elif t in ("ArrayBuffer",) or (isinstance(t, str) and t.endswith("Array")):
        if _bytes_contain_marker(shape.get("b64"), marker):
            hits.append({
                "path": path,
                "value": f"<{t} {shape.get('bytes')}B contains marker bytes>",
                "where": "bytes",
            })
            found = True
    elif t == "obj":
        for k, v in (shape.get("keys") or {}).items():
            if _walk(v, marker, f"{path}.{k}", hits, strings):
                found = True
    elif t == "arr":
        for i, v in enumerate(shape.get("items") or []):
            if _walk(v, marker, f"{path}[{i}]", hits, strings):
                found = True
    return found


def summarize_bridge(records: list[dict], marker: str, probe: list[dict]) -> dict:
    """Turn raw instrumentation records into a bridge-feasibility verdict."""
    by_kind: dict[str, int] = {}
    workers: list[str] = []
    marker_records: list[dict] = []

    for r in records or []:
        k = r.get("k", "?")
        by_kind[k] = by_kind.get(k, 0) + 1
        if k in ("worker_new", "sharedworker_new") and r.get("url"):
            workers.append(str(r["url"]))

        if k not in _BOUNDARY_KINDS:
            continue
        shape = r.get("shape")
        hits: list[dict] = []
        strings: list[str] = []
        if _walk(shape, marker, k, hits, strings):
            top_keys = []
            ctor = None
            if isinstance(shape, dict) and shape.get("t") == "obj":
                top_keys = list((shape.get("keys") or {}).keys())
                ctor = shape.get("ctor")
            # keep short, name-like strings (method names, field labels)
            near = [s for s in dict.fromkeys(strings) if s and s != marker][:30]
            marker_records.append({
                "channel": k,
                "url": r.get("url"),
                "ctor": ctor,
                "top_keys": top_keys,
                "marker_paths": hits,
                "nearby_strings": near,
            })

    return {
        "marker": marker,
        "total_records": len(records or []),
        "by_kind": by_kind,
        "workers": sorted(set(workers)),
        "marker_found": bool(marker_records),
        "marker_records": marker_records,
        "probe": probe or [],
    }


def print_bridge_summary(summary: dict) -> None:
    """Print a self-contained verdict for the user to read/paste back."""
    LINE = "-" * 31
    print("")
    print("[bridge] ===== BRIDGE DISCOVERY SUMMARY =====")
    print(f"[bridge] {LINE}")
    print(f"[bridge] marker            : {summary.get('marker')}")
    print(f"[bridge] send status       : {summary.get('send_status', '?')}")
    print(f"[bridge] records captured  : {summary.get('total_records', 0)}")

    by_kind = summary.get("by_kind") or {}
    if by_kind:
        kinds = "  ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
        print(f"[bridge] by kind           : {kinds}")

    workers = summary.get("workers") or []
    if workers:
        print(f"[bridge] workers seen ({len(workers)}):")
        for u in workers[:12]:
            print(f"[bridge]    - {u[:110]}")
    else:
        print("[bridge] workers seen      : (none observed)")

    print(f"[bridge] {LINE}")
    if summary.get("marker_found"):
        print("[bridge] MARKER TEXT crossed a thread boundary in PLAINTEXT:  YES  ✅")
        for i, rec in enumerate(summary.get("marker_records", [])[:6], start=1):
            print(f"[bridge]   hit #{i}: channel={rec.get('channel')} "
                  f"ctor={rec.get('ctor')} url={str(rec.get('url') or '')[:70]}")
            for p in rec.get("marker_paths", [])[:4]:
                print(f"[bridge]       at {p.get('path')} ({p.get('where')})")
            if rec.get("top_keys"):
                print(f"[bridge]       payload keys : {rec.get('top_keys')[:20]}")
            if rec.get("nearby_strings"):
                print(f"[bridge]       nearby names : {rec.get('nearby_strings')[:16]}")
        print(f"[bridge] {LINE}")
        print("[bridge] VERDICT: BRIDGE IS FEASIBLE.")
        print("[bridge]   The high-level send (peer + text) reaches the worker boundary")
        print("[bridge]   unencrypted. We can post the same task directly -> hybrid engine")
        print("[bridge]   (bridge as fast primary + UI as reliable fallback).")
    else:
        print("[bridge] MARKER TEXT crossed a thread boundary in PLAINTEXT:  NO  ❌")
        print(f"[bridge] {LINE}")
        print("[bridge] VERDICT: no plaintext high-level send seen at the JS boundary.")
        print("[bridge]   The task is likely encoded/encrypted before postMessage, or uses")
        print("[bridge]   a channel we did not hook. Recommendation: build the fast UI engine")
        print("[bridge]   (fill search + exact peer_id click + insert text + Enter + real verify).")

    probe = summary.get("probe") or []
    print(f"[bridge] {LINE}")
    if probe:
        print(f"[bridge] window manager probe ({len(probe)} candidate objects):")
        for p in probe[:12]:
            if p.get("methods"):
                print(f"[bridge]    - {p.get('path')} -> {p.get('methods')}")
            else:
                print(f"[bridge]    - {p.get('path')} ({p.get('kind')})")
    else:
        print("[bridge] window manager probe : nothing callable on window "
              "(expected if managers live in the worker)")

    if summary.get("run_dir"):
        print(f"[bridge] raw evidence saved : {summary['run_dir']}")
    print("[bridge] ======================================")
