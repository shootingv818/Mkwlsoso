#!/usr/bin/env python3
"""
PDF LAYOUT PROBE -- why did a PDF built from 16 images show only 4?

The evidence says the data was fine: 271 KB of base64 for 16 images is ~17 KB
each, which decodes to ~12.4 KB and matches the 'm' size exactly (12,231 bytes),
and the finished PDF was 216 KB -- about 16 x 12 KB. So all sixteen images were
embedded and the problem is LAYOUT, not fetching.

The suspect is CSS Grid. Chromium's print path fragments grid containers badly,
so items past the first page get clipped instead of flowing onto page 2. This
probe does not argue about it -- it renders the same images four ways and counts
the pages each one produces:

    grid          the layout that was used (suspect)
    inline-block  fragments across pages reliably
    float         the old-fashioned way
    table         rows are natural break points

For every layout it reports the content height, how many pages that height
SHOULD need, how many pages the PDF actually has, and whether images were
dropped. A layout that needs 3 pages but produces 1 is clipping.

It also verifies that every image is decoded before page.pdf() is called, which
is a second, independent reason images can come out blank.

Read-only against Eitaa: one search plus a few upload.getFile calls. PDFs are
written under ARTIFACTS_DIR.

Usage:
    cd ~/Mkwlsoso
    .venv/bin/python probe_pdf.py 989124089268
    .venv/bin/python probe_pdf.py 989124089268 --images 32
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONUNBUFFERED", "1")

from config import config  # noqa: E402
from capture.pool import pool as session_pool  # noqa: E402
from eitaa.driver import EitaaDriver  # noqa: E402

config.ensure_dirs()

OUT = Path("/tmp/photo_probe_pdf.json")

# A4 at 96 CSS px per inch, minus an 8mm margin on each side.
A4_W_PX = 794
A4_H_PX = 1123
MARGIN_MM = 8
USABLE_H_PX = A4_H_PX - int(2 * MARGIN_MM * 96 / 25.4)

JS_FETCH = r"""
async (n) => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, error: 'no apiManager' };
  const S = v => String(v);

  // Find a chat with plenty of photos.
  let offset_date = 0, offset_id = 0, offset_peer = { _: 'inputPeerEmpty' };
  const users = new Map();
  let best = null, bestCount = 0;
  for (let page = 0; page < 3 && !(bestCount >= n); page++) {
    const d = await AM.invokeApi('messages.getDialogs', {
      folder_id: 0, offset_date, offset_id, offset_peer, limit: 100, hash: 0 });
    const dl = (d && d.dialogs) || [], us = (d && d.users) || [],
          ms = (d && d.messages) || [];
    for (const u of us) if (u && u.id != null) users.set(S(u.id), u);
    for (const dlg of dl) {
      const p = dlg.peer || {};
      if (p._ !== 'peerUser') continue;
      const u = users.get(S(p.user_id));
      if (!u || u.access_hash == null) continue;
      const f = u.pFlags || {};
      if (f.self || f.bot || f.deleted) continue;
      try {
        const r = await AM.invokeApi('messages.search', {
          peer: { _:'inputPeerUser', user_id:+u.id, access_hash:u.access_hash },
          q:'', filter:{_:'inputMessagesFilterPhotos'}, min_date:0, max_date:0,
          offset_id:0, add_offset:0, limit:100, max_id:0, min_id:0, hash:0 });
        const ph = ((r && r.messages)||[]).filter(m => m.media && m.media.photo);
        if (ph.length > bestCount) {
          bestCount = ph.length;
          best = { name: ((u.first_name||'')+' '+(u.last_name||'')).trim() || S(u.id),
                   photos: ph.map(m => m.media.photo) };
        }
        if (bestCount >= n) break;
      } catch (e) {}
    }
    if (dl.length === 0) break;
    const last = dl[dl.length - 1];
    const topId = last.top_message || 0;
    const lm = ms.find(m => m.id === topId);
    const prev = offset_id;
    offset_id = topId; offset_date = lm ? lm.date : offset_date;
    const lp = last.peer || {};
    if (lp._ === 'peerUser') {
      const u = users.get(S(lp.user_id));
      offset_peer = u && u.access_hash != null
        ? { _:'inputPeerUser', user_id:+u.id, access_hash:u.access_hash }
        : { _:'inputPeerEmpty' };
    } else break;
    if (offset_peer._ === 'inputPeerEmpty' || offset_id === prev) break;
  }
  if (!best) return { ok: false, error: 'no chat with photos found' };

  const pick = p => {
    const s = (p.sizes||[]).filter(x => x.type && x.w)
      .sort((a,b) => Math.abs(a.w-320) - Math.abs(b.w-320));
    return s.length ? s[0] : null;
  };
  const want = best.photos.slice(0, n);
  const res = await Promise.all(want.map(async p => {
    try {
      const sz = pick(p);
      const r = await AM.invokeApi('upload.getFile', {
        location: { _:'inputPhotoFileLocation', id:p.id, access_hash:p.access_hash,
                    file_reference:p.file_reference, thumb_size: sz.type },
        offset:0, limit:1048576 });
      const u8 = r && r.bytes ? new Uint8Array(r.bytes) : new Uint8Array(0);
      if (!u8.length) return null;
      let s2 = ''; const CH = 8192;
      for (let i = 0; i < u8.length; i += CH)
        s2 += String.fromCharCode.apply(null, u8.subarray(i, i+CH));
      return { b64: btoa(s2), w: sz.w, h: sz.h };
    } catch (e) { return null; }
  }));
  const good = res.filter(Boolean);
  return { ok: good.length > 0, chat: best.name, available: bestCount,
           fetched: good.length, images: good };
}
"""

LAYOUTS = {
    "grid": (
        ".wrap{display:grid;grid-template-columns:repeat(3,1fr);gap:4mm}"
        ".cell{break-inside:avoid;text-align:center}"
    ),
    "inline_block": (
        ".wrap{font-size:0}"
        ".cell{display:inline-block;width:32%;margin:0.6%;"
        "break-inside:avoid;page-break-inside:avoid;text-align:center;"
        "vertical-align:top}"
    ),
    "float": (
        ".wrap{}"
        ".wrap:after{content:'';display:block;clear:both}"
        ".cell{float:left;width:32%;margin:0.6%;"
        "break-inside:avoid;page-break-inside:avoid;text-align:center}"
    ),
    "table": (
        ".wrap{display:table;width:100%;border-collapse:collapse}"
        ".row{display:table-row;break-inside:avoid;page-break-inside:avoid}"
        ".cell{display:table-cell;width:33%;padding:2mm;text-align:center;"
        "vertical-align:top}"
    ),
}


def build_html(images: list[dict], layout: str) -> str:
    css = LAYOUTS[layout]
    if layout == "table":
        rows = []
        for i in range(0, len(images), 3):
            cells = "".join(
                f'<div class="cell"><img src="data:image/jpeg;base64,{im["b64"]}">'
                f'<div class="lbl">{n + i + 1}</div></div>'
                for n, im in enumerate(images[i:i + 3]))
            rows.append(f'<div class="row">{cells}</div>')
        body = "".join(rows)
    else:
        body = "".join(
            f'<div class="cell"><img src="data:image/jpeg;base64,{im["b64"]}">'
            f'<div class="lbl">{i + 1}</div></div>'
            for i, im in enumerate(images))
    return (
        "<html><head><meta charset='utf-8'><style>"
        f"@page{{size:A4;margin:{MARGIN_MM}mm}}"
        "html,body{margin:0;padding:0}"
        "img{width:100%;height:auto;display:block;border:1px solid #ccc}"
        ".lbl{font:10px sans-serif;color:#333}"
        f"{css}"
        "</style></head><body>"
        f"<div class='wrap'>{body}</div>"
        "</body></html>"
    )


def pdf_page_count(path: Path) -> int:
    """Count pages without a PDF library: Chromium leaves the page objects
    uncompressed, so counting /Type /Page entries is reliable enough here."""
    try:
        raw = path.read_bytes()
    except OSError:
        return -1
    n = len(re.findall(rb"/Type\s*/Page[^s]", raw))
    if n:
        return n
    m = re.search(rb"/Count\s+(\d+)", raw)
    return int(m.group(1)) if m else -1


async def render(images: list[dict], layout: str) -> dict:
    from playwright.async_api import async_playwright
    html = build_html(images, layout)
    dest = Path(config.ARTIFACTS_DIR) / f"pdf_layout_{layout}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    info: dict = {"layout": layout, "images_in_html": len(images),
                  "html_kb": round(len(html) / 1024)}
    t0 = time.time()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(
                    viewport={"width": A4_W_PX, "height": A4_H_PX})
                await page.set_content(html, wait_until="load")
                # Do not trust "load": wait until every image is really decoded.
                try:
                    await page.wait_for_function(
                        "() => Array.from(document.images)"
                        ".every(i => i.complete && i.naturalWidth > 0)",
                        timeout=20000)
                    info["all_images_decoded"] = True
                except Exception:
                    info["all_images_decoded"] = False
                metrics = await page.evaluate(
                    "() => ({ decoded: Array.from(document.images)"
                    ".filter(i => i.complete && i.naturalWidth > 0).length,"
                    " total: document.images.length,"
                    " scrollH: document.documentElement.scrollHeight,"
                    " wrapH: (document.querySelector('.wrap')||{}).scrollHeight || 0 })")
                info.update(images_decoded=metrics["decoded"],
                            images_in_dom=metrics["total"],
                            content_height_px=metrics["scrollH"])
                await page.pdf(path=str(dest), format="A4",
                               margin={"top": f"{MARGIN_MM}mm",
                                       "bottom": f"{MARGIN_MM}mm",
                                       "left": f"{MARGIN_MM}mm",
                                       "right": f"{MARGIN_MM}mm"},
                               print_background=True)
            finally:
                await browser.close()
    except Exception as exc:
        info.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        return info
    size = dest.stat().st_size if dest.is_file() else 0
    pages = pdf_page_count(dest)
    expected = max(1, math.ceil(info.get("content_height_px", 0) / USABLE_H_PX))
    info.update(ok=size > 1000, pdf_kb=round(size / 1024),
                pdf_pages=pages, pages_expected=expected,
                clipped=(pages >= 0 and pages < expected),
                render_ms=round((time.time() - t0) * 1000),
                path=str(dest))
    return info


async def main(account: str, want: int) -> int:
    print("=" * 70)
    print(f"  PDF LAYOUT PROBE -- {account}, target {want} images")
    print("=" * 70)
    print()
    print(f"  A4 usable height: {USABLE_H_PX} px "
          f"(margin {MARGIN_MM}mm each side)")
    print()

    result: dict = {"steps": {}}
    t0 = time.time()
    async with session_pool.lease(account, headed=config.HEADED_JOBS) as session:
        driver = EitaaDriver(session)
        await driver.open()
        print(f"  session ready in {time.time() - t0:.1f}s")
        if not await driver.is_logged_in():
            print("  FATAL: not logged in")
            return 1
        print(f"  fetching up to {want} photos ...")
        fetched = await driver.page.evaluate(JS_FETCH, want)

    if not fetched or not fetched.get("ok"):
        print(f"  fetch failed: {fetched}")
        return 1

    images = fetched["images"]
    print(f"  chat used      : {fetched.get('chat')}")
    print(f"  photos in chat : {fetched.get('available')}")
    print(f"  photos fetched : {len(images)}")
    if images:
        print(f"  image size     : {images[0]['w']}x{images[0]['h']}")
    print()
    result["steps"]["fetch"] = {"chat": fetched.get("chat"),
                                "available": fetched.get("available"),
                                "fetched": len(images)}

    rows = []
    for layout in ("grid", "inline_block", "float", "table"):
        print(f"  ... rendering {layout}")
        info = await render(images, layout)
        rows.append(info)
        result["steps"][f"layout_{layout}"] = info
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                  default=str), encoding="utf-8")
        for k in ("images_decoded", "images_in_dom", "content_height_px",
                  "pages_expected", "pdf_pages", "clipped", "pdf_kb",
                  "render_ms", "error"):
            if k in info:
                print(f"        {k}: {info[k]}")
        print()

    print("-" * 70)
    print("  COMPARISON")
    print()
    print(f"    {'layout':14} {'decoded':>8} {'height':>8} {'need':>5} "
          f"{'pages':>6} {'clipped':>8} {'kb':>6} {'ms':>6}")
    for r in rows:
        print(f"    {r['layout']:14} "
              f"{r.get('images_decoded', '-'):>8} "
              f"{r.get('content_height_px', '-'):>8} "
              f"{r.get('pages_expected', '-'):>5} "
              f"{r.get('pdf_pages', '-'):>6} "
              f"{str(r.get('clipped', '-')):>8} "
              f"{r.get('pdf_kb', '-'):>6} "
              f"{r.get('render_ms', '-'):>6}")
    print()

    good = [r for r in rows if r.get("ok") and not r.get("clipped")]
    bad = [r for r in rows if r.get("clipped")]
    if bad:
        print("    CLIPPING (images past page 1 are lost):")
        for r in bad:
            print(f"      {r['layout']}: needed {r.get('pages_expected')} pages, "
                  f"got {r.get('pdf_pages')}")
    if good:
        best = min(good, key=lambda r: r.get("render_ms", 1e9))
        print(f"    SAFE layouts: {', '.join(r['layout'] for r in good)}")
        print(f"    fastest safe : {best['layout']} "
              f"({best.get('render_ms')} ms for {len(images)} images, "
              f"{best.get('pdf_pages')} pages)")
    else:
        print("    no layout paginated correctly -- needs a different approach")
    print()
    print("    open the PDFs to confirm visually:")
    for r in rows:
        if r.get("path"):
            print(f"      {r['path']}")
    print()
    print(f"  full JSON: {OUT}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not argv:
        print(f"usage: {sys.argv[0]} <account> [--images N]")
        sys.exit(1)
    n = 16
    if "--images" in sys.argv:
        try:
            n = int(sys.argv[sys.argv.index("--images") + 1])
        except (IndexError, ValueError):
            pass
    try:
        sys.exit(asyncio.run(main(argv[0], n)))
    except KeyboardInterrupt:
        sys.exit(130)
