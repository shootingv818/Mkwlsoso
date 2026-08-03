"""The photo export job: scan, download, render, deliver.

Nothing is sent to anybody on Eitaa. The only writes are the PDF files under
ARTIFACTS_DIR and the documents delivered to the owner's own Telegram chat.

Every rate used to pace the run was measured on the live account:
  scan  ~55 ms per chat at concurrency 8
  fetch ~30 ms per photo at concurrency 16, no FLOOD_WAIT over 48 downloads
  pdf   ~90-120 ms per page
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from config import config

from . import cards as px_cards
from . import fetcher, renderer, scanner

# Defaults; the panel can override the direction and the cap.
MAX_PHOTOS = int(getattr(config, "PHOTO_EXPORT_MAX", 0) or 500)
PER_FILE = int(getattr(config, "PHOTO_EXPORT_PER_FILE", 0) or 150)
TARGET_WIDTH = int(getattr(config, "PHOTO_EXPORT_WIDTH", 0) or 320)
SCAN_CONC = 8
# The probe measured 16 clean on a quiet account (63 photos in one chat), but an
# account with 4,853 photos across 171 chats refused it right away -- the scan had
# already spent the budget. Start at 8 and let the fetcher halve on each flood.
FETCH_CONC = int(getattr(config, "PHOTO_EXPORT_CONC", 0) or 8)
FETCH_BATCH = 24


def _when(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


async def export(driver, account: str, phone: str, *, direction: str = "both",
                 report: Callable[[str], Awaitable[None]],
                 live=None,
                 send_document: Callable[..., Awaitable[object]] | None = None,
                 should_stop: Callable[[], bool] | None = None,
                 max_photos: int = 0, target_width: int = 0) -> dict:
    """Run one export. Returns a summary dict; never raises for a data problem."""
    started = time.time()
    cap = max_photos or MAX_PHOTOS
    width = target_width or TARGET_WIDTH
    state = {
        "account": account, "phone": phone, "direction": direction,
        "status": "STARTING", "step": "PREPARING",
        "chats_total": 0, "chats_scanned": 0, "photos_found": 0,
        "photos_target": 0, "downloaded": 0,
        "pages_built": 0, "pages_total": 0, "files_sent": 0, "files_total": 0,
        "note": None,
    }

    async def paint(force: bool = False) -> None:
        if live is None:
            return
        try:
            await live.set(px_cards.progress(
                account=account, phone=phone, direction=direction,
                status=state["status"], step=state["step"],
                chats_total=state["chats_total"],
                chats_scanned=state["chats_scanned"],
                photos_found=state["photos_found"],
                photos_target=state["photos_target"],
                downloaded=state["downloaded"],
                pages_built=state["pages_built"],
                pages_total=state["pages_total"],
                files_sent=state["files_sent"],
                files_total=state["files_total"],
                elapsed=time.time() - started,
                note=state["note"]), force=force)
        except Exception:  # noqa: BLE001 - a card must never break the job
            pass

    # ---- bridge ---------------------------------------------------------
    state.update(status="PREPARING", step="INJECT BRIDGE")
    await paint(True)
    if not await scanner.ensure_bridge(driver):
        return {"ok": False, "code": "bridge_unavailable",
                "detail": "the in-page photo bridge could not be injected"}
    await scanner.reset(driver)

    # ---- chats ----------------------------------------------------------
    state.update(status="SCANNING", step="LIST CHATS")
    await paint(True)
    chats = await scanner.list_chats(driver)
    if not chats or not chats.get("ok"):
        return {"ok": False, "code": (chats or {}).get("code", "dialogs_failed")}
    state["chats_total"] = int(chats.get("peers") or 0)
    await paint(True)
    if not state["chats_total"]:
        return {"ok": True, "photos": 0, "chats_total": 0, "files": [],
                "elapsed": time.time() - started}

    # ---- scan for photos ------------------------------------------------
    state.update(step="SCAN PHOTOS")

    async def on_scan(scanned: int, found: int) -> None:
        state["chats_scanned"] = scanned
        state["photos_found"] = found
        await paint()

    sc = await scanner.scan(
        driver, total_chats=state["chats_total"], conc=SCAN_CONC,
        on_progress=on_scan, should_stop=should_stop)
    if not sc.get("ok"):
        return {"ok": False, "code": sc.get("code", "scan_failed"),
                "chats_total": state["chats_total"]}
    if sc.get("floods"):
        state["note"] = "Eitaa asked us to slow down during the scan"
    stopped = bool(sc.get("stopped"))

    meta = await scanner.metadata(driver)
    chosen = scanner.select(meta, direction, cap)
    state["photos_found"] = len(meta)
    state["photos_target"] = len(chosen)
    chats_with_photos = len({m.get("chat") for m in meta if m.get("chat")})
    await paint(True)

    if not chosen:
        return {"ok": True, "photos": 0, "chats_total": state["chats_total"],
                "chats_with_photos": chats_with_photos, "files": [],
                "elapsed": time.time() - started, "stopped": stopped,
                "nothing_found": True}

    # ---- download -------------------------------------------------------
    state.update(status="DOWNLOADING", step="FETCH PHOTOS")
    await paint(True)

    async def on_fetch(done: int, total: int) -> None:
        state["downloaded"] = done
        await paint()

    async def on_wait(seconds: int, new_conc: int) -> None:
        state["status"] = "WAITING"
        state["step"] = f"RATE LIMIT - PAUSE {seconds}s"
        state["note"] = (f"Eitaa asked for {seconds}s; continuing at "
                         f"concurrency {new_conc}")
        await paint(True)

    got = await fetcher.fetch(
        driver, [int(m["i"]) for m in chosen], target_width=width,
        conc=FETCH_CONC, batch=FETCH_BATCH,
        on_progress=on_fetch, on_wait=on_wait, should_stop=should_stop)
    if not got.get("ok"):
        return {"ok": False, "code": got.get("code", "fetch_failed"),
                "chats_total": state["chats_total"]}
    stopped = stopped or bool(got.get("stopped"))
    state.update(status="DOWNLOADING", step="FETCH PHOTOS")

    by_index = got.get("images") or {}
    items: list[dict] = []
    for m in chosen:
        img = by_index.get(int(m["i"]))
        if not img:
            continue
        items.append({"bytes": img["bytes"], "chat": m.get("chat"),
                      "when": _when(m.get("date") or 0), "out": m.get("out")})
    skipped = len(chosen) - len(items)
    state["downloaded"] = len(items)

    # Be honest about a download the server cut short.
    partial = bool(got.get("gave_up")) or skipped > 0
    if got.get("gave_up"):
        state["note"] = (
            f"Eitaa kept rate-limiting: {len(items)} of {len(chosen)} photos "
            f"were downloaded. Run it again to collect the rest.")
    elif got.get("waited"):
        state["note"] = (f"waited {got['waited']}s for Eitaa's rate limit; "
                         f"finished at concurrency {got.get('conc_final')}")
    elif skipped:
        state["note"] = f"{skipped} photo(s) could not be downloaded"

    if not items:
        return {"ok": False, "code": "no_photo_downloaded",
                "chats_total": state["chats_total"]}

    # ---- render ---------------------------------------------------------
    state.update(status="BUILDING", step="RENDER PDF")
    state["pages_total"] = len(items)
    await paint(True)

    out_dir = Path(config.ARTIFACTS_DIR) / "photo_export"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"photos_{account}_{direction}_{stamp}"
    rendered = await renderer.render(items, out_dir, base_name=base,
                                     per_file=PER_FILE)
    if not rendered.get("ok"):
        return {"ok": False, "code": rendered.get("code", "render_failed"),
                "chats_total": state["chats_total"]}
    files = rendered.get("files") or []
    state["pages_built"] = rendered.get("pages") or len(items)
    state["files_total"] = len(files)
    await paint(True)

    # ---- deliver --------------------------------------------------------
    delivered: list[dict] = []
    if send_document is not None:
        state.update(status="SENDING", step="UPLOAD TO TELEGRAM")
        await paint(True)
        for f in files:
            try:
                await send_document(
                    f["path"],
                    caption=(f"photos - {phone} - {direction} - "
                             f"{f['pages']} page(s)"))
                delivered.append(f)
            except Exception as exc:  # noqa: BLE001
                state["note"] = f"delivery failed for {f['name']}: {exc}"
            state["files_sent"] = len(delivered)
            await paint(True)

    state.update(status="PARTIAL" if partial else "DONE", step="FINISHED")
    # The bars are what the owner reads first, so make them agree with reality:
    # a run that only got 15 of 500 must not paint a full Download bar.
    state["photos_target"] = max(state["photos_target"], len(items))
    await paint(True)

    sent_by_me = sum(1 for it in items if it.get("out"))
    return {
        "ok": True, "photos": len(items), "sent_by_me": sent_by_me,
        "received": len(items) - sent_by_me,
        "chats_total": state["chats_total"],
        "chats_with_photos": chats_with_photos,
        "photos_available": len(meta),
        "requested": len(chosen),
        "files": files, "delivered": len(delivered), "skipped": skipped,
        "elapsed": time.time() - started, "stopped": stopped,
        "partial": partial, "rate_limited": bool(got.get("floods")),
        "waited": got.get("waited") or 0,
        "render_ms": rendered.get("ms"),
        "ms_per_page": rendered.get("ms_per_page"),
        "out_dir": str(out_dir),
        "note": state["note"],
    }
