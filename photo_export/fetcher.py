"""Download the selected photos out of the page, in batches.

Measured on the live account: upload.getFile is fastest with NO dc options, and
concurrency 16 sustained 48 downloads at ~30 ms each with zero FLOOD_WAIT. The
batch size is what keeps memory sane -- base64 is 33% larger than the bytes, so
handing 1,400 images across the bridge in one call would mean a ~20 MB string.
"""

from __future__ import annotations

import base64
from typing import Awaitable, Callable


async def fetch(driver, indexes: list[int], *, target_width: int = 320,
                conc: int = 16, batch: int = 24,
                on_progress: Callable[[int, int], Awaitable[None]] | None = None,
                should_stop: Callable[[], bool] | None = None) -> dict:
    """Return {ok, images, failed, floods, stopped}.

    `images` is aligned with `indexes`: each entry is either a dict with the raw
    bytes or None when that photo could not be fetched.
    """
    out: list[dict | None] = []
    failed = floods = 0
    stopped = False
    total = len(indexes)

    for start in range(0, total, batch):
        if should_stop is not None and should_stop():
            stopped = True
            break
        chunk = indexes[start:start + batch]
        try:
            res = await driver.page.evaluate(
                "(a) => window.__MKWL_px_fetch(a.idx, a.opts)",
                {"idx": chunk,
                 "opts": {"targetWidth": target_width, "conc": conc}})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"{type(exc).__name__}: {exc}",
                    "images": out, "failed": failed, "floods": floods}
        if not res or not res.get("ok"):
            return {"ok": False, "code": (res or {}).get("code", "fetch failed"),
                    "images": out, "failed": failed, "floods": floods}

        for item in (res.get("images") or []):
            if not item or not item.get("b64"):
                out.append(None)
                continue
            try:
                raw = base64.b64decode(item["b64"])
            except Exception:  # noqa: BLE001 - a bad image is skipped, not fatal
                out.append(None)
                failed += 1
                continue
            out.append({"bytes": raw, "w": item.get("w"), "h": item.get("h")})

        failed += int(res.get("failed") or 0)
        floods += int(res.get("floods") or 0)
        if on_progress is not None:
            await on_progress(len(out), total)
        if floods:
            break

    return {"ok": True, "images": out, "failed": failed, "floods": floods,
            "stopped": stopped}
