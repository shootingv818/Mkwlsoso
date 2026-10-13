"""Turn the downloaded photos into PDFs, ONE PHOTO PER PAGE.

Two things were learned by measurement and are enforced here:

  * `page.pdf()` only works in headless Chromium, and jobs run headed
    (DISPLAY=:99), so a separate headless browser is launched purely to render.
  * `set_content(wait_until="load")` does NOT guarantee that data: URI images
    have decoded. The render waits until every image reports complete with a
    non-zero naturalWidth before calling pdf(), otherwise a partially painted
    document can be captured.

Rendering was measured at roughly 90-120 ms per image, so a big export is split
across several files instead of building one enormous HTML string in memory.
"""

from __future__ import annotations

import base64
import html
import time
from pathlib import Path

# A4 at 96 CSS px per inch.
_A4_W = 794
_A4_H = 1123
_MARGIN_MM = 6


def _page_html(items: list[dict]) -> str:
    """One photo per printed page, with its caption under it."""
    pages = []
    for i, it in enumerate(items, start=1):
        b64 = base64.b64encode(it["bytes"]).decode("ascii")
        chat = html.escape(str(it.get("chat") or ""))[:60]
        when = html.escape(str(it.get("when") or ""))
        arrow = "sent" if it.get("out") else "received"
        pages.append(
            "<section class='pg'>"
            f"<div class='img'><img src='data:image/jpeg;base64,{b64}'></div>"
            f"<div class='cap'><b>{i}</b> &middot; {chat} &middot; {when} "
            f"&middot; {arrow}</div>"
            "</section>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        f"@page{{size:A4;margin:{_MARGIN_MM}mm}}"
        "html,body{margin:0;padding:0;background:#fff}"
        # Each section is exactly one page. break-after:page is the modern
        # property; page-break-after is kept for older print engines.
        ".pg{break-after:page;page-break-after:always;"
        "height:281mm;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;overflow:hidden}"
        ".pg:last-child{break-after:auto;page-break-after:auto}"
        # The image fills the page but is never cropped or upscaled past its
        # own resolution.
        ".img{flex:1 1 auto;display:flex;align-items:center;"
        "justify-content:center;width:100%;min-height:0}"
        ".img img{max-width:100%;max-height:100%;width:auto;height:auto;"
        "object-fit:contain;display:block}"
        ".cap{flex:0 0 auto;padding-top:3mm;font:11px/1.4 sans-serif;"
        "color:#333;text-align:center;direction:rtl}"
        "</style></head><body>"
        + "".join(pages) +
        "</body></html>"
    )


async def render(items: list[dict], out_dir: Path, *, base_name: str,
                 per_file: int = 150) -> dict:
    """Render `items` to one or more PDFs, one photo per page.

    Each item: {bytes, chat, when, out}. Returns
    {ok, files: [{path, name, pages, kb}], pages, ms, code}.
    """
    from playwright.async_api import async_playwright

    usable = [it for it in items if it and it.get("bytes")]
    if not usable:
        return {"ok": False, "code": "no images to render", "files": []}

    out_dir.mkdir(parents=True, exist_ok=True)
    groups = [usable[i:i + per_file] for i in range(0, len(usable), per_file)]
    files: list[dict] = []
    t0 = time.time()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                for n, group in enumerate(groups, start=1):
                    suffix = "" if len(groups) == 1 else f"_{n}of{len(groups)}"
                    dest = out_dir / f"{base_name}{suffix}.pdf"
                    page = await browser.new_page(
                        viewport={"width": _A4_W, "height": _A4_H})
                    try:
                        await page.set_content(_page_html(group),
                                               wait_until="load")
                        # Never call pdf() on a half-painted document.
                        try:
                            await page.wait_for_function(
                                "() => Array.from(document.images)"
                                ".every(i => i.complete && i.naturalWidth > 0)",
                                timeout=60000)
                        except Exception:  # noqa: BLE001
                            pass
                        await page.pdf(
                            path=str(dest), format="A4",
                            print_background=True,
                            margin={"top": f"{_MARGIN_MM}mm",
                                    "bottom": f"{_MARGIN_MM}mm",
                                    "left": f"{_MARGIN_MM}mm",
                                    "right": f"{_MARGIN_MM}mm"})
                    finally:
                        await page.close()
                    size = dest.stat().st_size if dest.is_file() else 0
                    files.append({"path": str(dest), "name": dest.name,
                                  "pages": len(group),
                                  "kb": round(size / 1024)})
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"{type(exc).__name__}: {exc}",
                "files": files}

    ms = round((time.time() - t0) * 1000)
    return {"ok": bool(files), "files": files, "pages": len(usable), "ms": ms,
            "ms_per_page": round(ms / max(1, len(usable)))}
