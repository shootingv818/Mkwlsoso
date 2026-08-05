"""Find the photos: walk the private chats, then walk each chat's photos.

Both walks stop on the same evidence, because this build of Eitaa lies about
completeness in two different ways:

  * getDialogs answers 25 on the first page and 100 after that, so a page
    shorter than the limit does NOT mean the end. Only an EMPTY page does.
  * messages.search never fills `count` and returns the "complete" type even
    when it hands back exactly the limit, so photo paging walks until a SHORT
    page instead of believing the reply.

Both rules live in `bridge.js`; this module drives it in slices so a long scan
can paint progress and honour a stop request.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

_BRIDGE = Path(__file__).with_name("bridge.js")

try:
    BRIDGE_SRC = _BRIDGE.read_text(encoding="utf-8")
except OSError:  # noqa: BLE001 - a missing bridge is reported, never raised
    BRIDGE_SRC = ""

_PROBE = "() => typeof window.__MKWL_px_dialogs === 'function'"


async def ensure_bridge(driver) -> bool:
    """Inject the in-page bridge if it is not there yet."""
    try:
        if await driver.page.evaluate(_PROBE):
            return True
    except Exception:  # noqa: BLE001
        pass
    if not BRIDGE_SRC:
        return False
    try:
        await driver.page.evaluate(BRIDGE_SRC)
        return await driver.page.evaluate(_PROBE)
    except Exception:  # noqa: BLE001
        return False


async def reset(driver) -> None:
    try:
        await driver.page.evaluate("() => window.__MKWL_px_reset()")
    except Exception:  # noqa: BLE001
        pass


async def list_chats(driver, max_pages: int = 40) -> dict:
    """Every private chat, bots and the self chat excluded."""
    try:
        return await driver.page.evaluate(
            "(n) => window.__MKWL_px_dialogs(n)", int(max_pages))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"{type(exc).__name__}: {exc}"}


async def scan(driver, *, total_chats: int, slice_size: int = 40,
               conc: int = 4, max_per_chat: int = 2000,
               min_date: int = 0, max_date: int = 0, delay: float = 0.0,
               on_progress: Callable[[int, int], Awaitable[None]] | None = None,
               should_stop: Callable[[], bool] | None = None) -> dict:
    """Scan every chat for photos, in slices, reporting progress.

    `delay` is a deliberate pause between slices. Scanning at full speed spends
    the account's rate-limit budget before the download even starts, which is how
    a run ended up collecting 15 photos of 500.

    Returns {ok, scanned, photos, floods, errors, stopped}.
    """
    scanned = photos = floods = errors = 0
    stopped = False
    at = 0
    first = True
    while at < total_chats:
        if should_stop is not None and should_stop():
            stopped = True
            break
        if delay > 0 and not first:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                stopped = True
                break
        first = False
        try:
            res = await driver.page.evaluate(
                "(a) => window.__MKWL_px_scan(a.from, a.count, a.opts)",
                {"from": at, "count": slice_size,
                 "opts": {"conc": conc, "maxPerChat": max_per_chat,
                          "min_date": min_date, "max_date": max_date}})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"{type(exc).__name__}: {exc}",
                    "scanned": scanned, "photos": photos}
        if not res or not res.get("ok"):
            return {"ok": False, "code": (res or {}).get("code", "scan failed"),
                    "scanned": scanned, "photos": photos}

        scanned = int(res.get("scanned") or 0)
        photos = int(res.get("total_photos") or 0)
        floods += int(res.get("floods") or 0)
        errors += int(res.get("errors") or 0)
        at += slice_size
        if on_progress is not None:
            await on_progress(scanned, photos)
        if floods:
            break
    return {"ok": True, "scanned": scanned, "photos": photos,
            "floods": floods, "errors": errors, "stopped": stopped}


async def metadata(driver) -> list[dict]:
    """Chat name, timestamp and direction for every collected photo."""
    try:
        res = await driver.page.evaluate("() => window.__MKWL_px_meta()")
    except Exception:  # noqa: BLE001
        return []
    return list((res or {}).get("photos") or [])


def select(meta: list[dict], direction: str, limit: int = 0) -> list[dict]:
    """Filter by direction and cap the count, newest first.

    `direction` is "sent", "received" or "both". Newest first means a capped
    export keeps the most recent photos, which is what an owner expects.
    """
    if direction == "sent":
        rows = [m for m in meta if m.get("out")]
    elif direction == "received":
        rows = [m for m in meta if not m.get("out")]
    else:
        rows = list(meta)
    rows.sort(key=lambda m: int(m.get("date") or 0), reverse=True)
    if limit and len(rows) > limit:
        rows = rows[:limit]
    return rows
