"""Download the selected photos out of the page, obeying the server.

The first version of this file abandoned the whole download on the first
FLOOD_WAIT: on a busy account that meant 15 photos of 500 and a card that
cheerfully said DONE. `bot/runner.py::_flood_wait` already says why that is
wrong -- "a short server-declared pause is an instruction to obey, not a reason
to abandon the remaining recipients" -- so this now does what the send loop does:

  * a flood is a pause. Wait the seconds the server named (capped by
    config.MAX_FLOOD_WAIT) and retry the SAME photos.
  * every flood halves the concurrency, down to a floor of 2. A concurrency that
    the account will not tolerate is not worth insisting on.
  * only give up when the server keeps refusing after several rounds, or asks for
    longer than MAX_FLOOD_WAIT, and then say so plainly instead of pretending the
    export finished.

Concurrency starts lower than the probe suggested. 16 ran clean on a quiet
account with 63 photos in one chat; the account with 4,853 photos across 171
chats refused it almost immediately, because the scan had already spent the
budget.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Awaitable, Callable

from config import config

FLOOR_CONC = 2
MAX_FLOOD_ROUNDS = 6


async def fetch(driver, indexes: list[int], *, target_width: int = 320,
                conc: int = 3, batch: int = 24, delay: float = 0.0,
                on_progress: Callable[[int, int], Awaitable[None]] | None = None,
                on_wait: Callable[[int, int], Awaitable[None]] | None = None,
                on_pace: Callable[[float], Awaitable[None]] | None = None,
                should_stop: Callable[[], bool] | None = None) -> dict:
    """Download every index, waiting out rate limits.

    Returns {ok, images, failed, floods, waited, stopped, gave_up, conc_final}
    where `images` is a dict keyed by the original index so a caller can tell
    exactly which photos arrived.
    """
    got: dict[int, dict] = {}
    remaining = list(indexes)
    total = len(indexes)
    failed = floods = waited = rounds = 0
    paced = 0.0
    stopped = gave_up = False
    max_wait = int(getattr(config, "MAX_FLOOD_WAIT", 90) or 90)

    stalls = 0
    first = True
    while remaining:
        if should_stop is not None and should_stop():
            stopped = True
            break

        # A deliberate pause between batches. Running flat out is what earned the
        # rate limit in the first place, so the export paces itself the way the
        # send loop paces itself with TEXT_SEND_DELAY.
        if delay > 0 and not first:
            if on_pace is not None:
                await on_pace(delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                stopped = True
                break
            paced += delay
        first = False

        before = len(remaining)
        chunk = remaining[:batch]
        try:
            res = await driver.page.evaluate(
                "(a) => window.__MKWL_px_fetch(a.idx, a.opts)",
                {"idx": chunk,
                 "opts": {"targetWidth": target_width, "conc": conc}})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"{type(exc).__name__}: {exc}",
                    "images": got, "failed": failed, "floods": floods,
                    "waited": waited, "conc_final": conc}
        if not res or not res.get("ok"):
            return {"ok": False, "code": (res or {}).get("code", "fetch failed"),
                    "images": got, "failed": failed, "floods": floods,
                    "waited": waited, "conc_final": conc}

        flood_here = False
        for row in (res.get("results") or []):
            idx = int(row.get("i"))
            if row.get("ok") and row.get("b64"):
                try:
                    got[idx] = {"bytes": base64.b64decode(row["b64"]),
                                "w": row.get("w"), "h": row.get("h")}
                except Exception:  # noqa: BLE001 - a bad image is skipped
                    failed += 1
                if idx in remaining:
                    remaining.remove(idx)
                continue
            if row.get("flood"):
                flood_here = True          # leave it in `remaining` to retry
                continue
            # A permanent failure for this photo; do not retry it forever.
            failed += 1
            if idx in remaining:
                remaining.remove(idx)

        if on_progress is not None:
            await on_progress(len(got), total)

        if not flood_here:
            rounds = 0
            # A round that resolved nothing and was not rate-limited means the
            # bridge is answering in a shape this code cannot use. Without this
            # guard the loop would spin forever on the same indexes.
            if len(remaining) == before:
                stalls += 1
                if stalls >= 3:
                    return {"ok": False, "code": "fetch_stalled",
                            "detail": "the bridge returned no usable result for "
                                      "the same photos three times",
                            "images": got, "failed": failed, "floods": floods,
                            "waited": waited, "conc_final": conc,
                            "missing": len(remaining)}
            else:
                stalls = 0
            continue

        # The server asked us to slow down.
        floods += 1
        rounds += 1
        asked = int(res.get("wait") or 0)
        if asked > max_wait:
            gave_up = True
            break
        if rounds > MAX_FLOOD_ROUNDS:
            gave_up = True
            break

        pause = asked if asked > 0 else min(max_wait, 5 * rounds)
        new_conc = max(FLOOR_CONC, conc // 2)
        if on_wait is not None:
            await on_wait(pause, new_conc)
        conc = new_conc
        try:
            await asyncio.sleep(pause)
        except asyncio.CancelledError:
            stopped = True
            break
        waited += pause

    return {"ok": True, "images": got, "failed": failed, "floods": floods,
            "waited": waited, "paced": round(paced, 1), "stopped": stopped,
            "gave_up": gave_up, "conc_final": conc, "missing": len(remaining)}
