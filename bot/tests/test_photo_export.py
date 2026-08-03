"""Tests for the photo export (photo_export/).

Run: python -m bot.tests.test_photo_export

No browser and no network: a fake page answers the bridge calls the way the live
account was measured to answer them, so the parts that actually caused bugs are
the parts under test:

  * getDialogs hands back 25 on the first page and 100 afterwards, and the walk
    must keep going until an EMPTY page. Believing a short page is what once
    reported 24 chats where there were 608.
  * messages.search leaves `count` null and answers with the "complete" type
    even when it fills the limit, so photo paging must walk until a SHORT page.
  * `returned == 0` is the signal that a chat holds no photos.
  * direction filtering comes from pFlags.out.
  * the PDF must contain one page per photo, and the render must wait for images
    to decode.

The PDF step needs a real headless Chromium, so it is skipped automatically when
Playwright or its browser is unavailable, and the rest still runs.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_photo_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")


def _stub_playwright_module() -> None:
    """Only stubs the module when it is genuinely absent."""
    try:
        import playwright.async_api  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    pkg = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    for name in ("BrowserContext", "CDPSession", "Page", "Locator", "Error"):
        setattr(api, name, type(name, (object,), {}))
    api.TimeoutError = type("TimeoutError", (Exception,), {})
    api.async_playwright = lambda: None
    pkg.async_api = api
    sys.modules.setdefault("playwright", pkg)
    sys.modules.setdefault("playwright.async_api", api)


_stub_playwright_module()

from photo_export import cards as px_cards  # noqa: E402
from photo_export import engine, fetcher, renderer, scanner  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# A 1x1 JPEG, so the renderer has something real to embed.
_JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0"
    "aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPDIzMv/AABEIAAEAAQMBIgACEQEDEQH/xAAfAA"
    "ABBQEBAQEBAQAAAAAAAAAAAQIDBAUGBwgJCgv/xAC1EAACAQMDAgQDBQUEBAAAAX0BAgMABBEFE"
    "iExQQYTUWEHInEUMoGRoQgjQrHBFVLR8CQzYnKCCQoLhwkNDg8QERITFBUWFxgZGhscHR4fICEiI"
    "yQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj9AQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVpbX"
    "F1eX2BhYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ent8fX5/AP/aAAwDAQACEQMRAD8A/v4oooo/"
    "/9k=")


# --------------------------------------------------------------------------
# A fake page that behaves like the measured account
# --------------------------------------------------------------------------

class FakePage:
    """Serves the bridge's JS entry points from Python.

    The real bridge runs in Chromium; here the same call sequence is answered so
    scanner/fetcher logic is what gets exercised.
    """

    def __init__(self, chats: list[dict], *, first_page: int = 25,
                 page_size: int = 100, search_limit: int = 100) -> None:
        self.chats = chats
        self.first_page = first_page
        self.page_size = page_size
        self.search_limit = search_limit
        self.bridge_injected = False
        self.dialog_pages: list[int] = []
        self.search_calls = 0
        self.fetch_calls = 0
        self._peers: list[dict] = []
        self._photos: list[dict] = []
        self._seen: set[str] = set()
        self._scanned = 0

    # -- driver.page.evaluate ------------------------------------------
    async def evaluate(self, js, arg=None):
        if "typeof window.__MKWL_px_dialogs" in js:
            return self.bridge_injected
        if js.strip().startswith("(() => {") or "__MKWL_px_dialogs = async" in js:
            self.bridge_injected = True
            return None
        if "__MKWL_px_reset" in js:
            self._peers, self._photos, self._seen, self._scanned = [], [], set(), 0
            return {"ok": True}
        if "__MKWL_px_dialogs(n)" in js:
            return self._dialogs()
        if "__MKWL_px_scan" in js:
            return self._scan(arg or {})
        if "__MKWL_px_meta" in js:
            return {"ok": True, "photos": [
                {"i": i, "chat": p["chat"], "date": p["date"], "out": p["out"],
                 "id": p["id"],
                 "sizes": [{"type": "s", "w": 83, "h": 90, "bytes": 2000},
                           {"type": "m", "w": 296, "h": 320, "bytes": 12231}]}
                for i, p in enumerate(self._photos)]}
        if "__MKWL_px_fetch" in js:
            return self._fetch(arg or {})
        # The real bridge source injection.
        self.bridge_injected = True
        return None

    def _dialogs(self):
        # Page one is short on purpose: 25 then 100s, exactly as measured.
        sizes, at = [], 0
        while at < len(self.chats):
            n = self.first_page if not sizes else self.page_size
            take = min(n, len(self.chats) - at)
            sizes.append(take)
            at += take
        sizes.append(0)                     # the empty page that ends the walk
        self.dialog_pages = sizes
        self._peers = list(self.chats)
        return {"ok": True, "peers": len(self._peers), "pages": len(sizes),
                "stop_reason": "empty page"}

    def _scan(self, arg):
        frm = int(arg.get("from") or 0)
        count = int(arg.get("count") or 0)
        found = 0
        for p in self._peers[frm:frm + count]:
            photos = p.get("photos") or []
            # Page in blocks of search_limit, like the bridge does.
            for start in range(0, len(photos), self.search_limit):
                self.search_calls += 1
                block = photos[start:start + self.search_limit]
                for ph in block:
                    if ph["id"] in self._seen:
                        continue
                    self._seen.add(ph["id"])
                    self._photos.append({**ph, "chat": p["name"]})
                    found += 1
                if len(block) < self.search_limit:
                    break
            self._scanned += 1
        return {"ok": True, "scanned": self._scanned, "found": found,
                "total_photos": len(self._photos), "floods": 0, "errors": 0,
                "done": (frm + count) >= len(self._peers)}

    def _fetch(self, arg):
        self.fetch_calls += 1
        idx = arg.get("idx") or []
        b64 = base64.b64encode(_JPEG_1PX).decode("ascii")
        return {"ok": True, "failed": 0, "floods": 0,
                "images": [{"b64": b64, "bytes": len(_JPEG_1PX),
                            "w": 296, "h": 320} for _ in idx]}


class FakeDriver:
    def __init__(self, page):
        self.page = page


def make_chats(n_chats: int, photos_per: dict[int, int]) -> list[dict]:
    """`photos_per` maps chat index -> how many photos it holds."""
    out = []
    pid = 0
    for i in range(n_chats):
        photos = []
        for k in range(photos_per.get(i, 0)):
            pid += 1
            photos.append({"id": f"p{pid}", "date": 1700000000 + pid,
                           "out": (k % 2 == 0), "msg_id": 1000 + pid})
        out.append({"id": str(100 + i), "access_hash": "h",
                    "name": f"chat{i}", "top_message": 5000 + i,
                    "photos": photos})
    return out


class FakeLive:
    def __init__(self):
        self.texts: list[str] = []

    async def set(self, text, force=False):
        self.texts.append(text)

    async def flush(self):
        return None


# --------------------------------------------------------------------------
# 1. the bars and the card shape
# --------------------------------------------------------------------------

def test_cards() -> None:
    print("\n1) the live card")
    b = px_cards.bar(0, 10)
    check("an empty bar is all light shade", b == "[" + "\u2591" * 10 + "]", b)
    b = px_cards.bar(10, 10)
    check("a finished bar is all full block", b == "[" + "\u2588" * 10 + "]", b)
    b = px_cards.bar(10, 11)
    check("never full while work remains", b.count("\u2588") == 9, b)

    text = px_cards.progress(
        account="989124089268", phone="989124089268", direction="both",
        status="SCANNING", step="SCAN PHOTOS", chats_total=601,
        chats_scanned=250, photos_found=700, photos_target=500,
        downloaded=120, elapsed=42.0)
    for needle in ("| \u2699 - #photos", "--| Phone - 989124089268",
                   "\u2022 Status : SCANNING", "\u2022 Step : SCAN PHOTOS",
                   "\u2022 Overall", "\u2022 Scan Chats",
                   "\u2022 Download Photos", "\u2022 Build PDF",
                   "\u2022 Send Files", "Worker :"):
        check(f"card contains {needle!r}", needle in text)
    check("the divider is the project's own",
          text.count("-------------------------------") == 2)
    check("waiting stages say WAITING", "WAITING" in text)


# --------------------------------------------------------------------------
# 2. dialog paging must not stop on a short page
# --------------------------------------------------------------------------

def test_dialog_paging() -> None:
    print("\n2) dialog paging (the 24-vs-608 bug)")
    chats = make_chats(608, {})
    page = FakePage(chats)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    res = run(scanner.list_chats(drv))
    check("every chat is found", res.get("peers") == 608, str(res.get("peers")))
    check("page one really was short", page.dialog_pages[0] == 25,
          str(page.dialog_pages[:3]))
    check("the walk ended on an empty page", page.dialog_pages[-1] == 0,
          str(page.dialog_pages))


# --------------------------------------------------------------------------
# 3. photo scanning: skip empty chats, page the full ones
# --------------------------------------------------------------------------

def test_scan() -> None:
    print("\n3) scanning for photos")
    # 20 chats: one holds 250 photos (needs 3 pages), one holds 63, rest empty.
    chats = make_chats(20, {3: 250, 7: 63})
    page = FakePage(chats)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    run(scanner.list_chats(drv))

    seen: list[tuple[int, int]] = []

    async def on_progress(scanned, found):
        seen.append((scanned, found))

    res = run(scanner.scan(drv, total_chats=20, slice_size=5,
                           on_progress=on_progress))
    check("scan reports ok", res.get("ok") is True, str(res))
    check("all 20 chats were scanned", res.get("scanned") == 20,
          str(res.get("scanned")))
    check("every photo was collected", res.get("photos") == 313,
          f"{res.get('photos')} (expected 250 + 63)")
    check("progress was reported per slice", len(seen) == 4, str(seen))

    meta = run(scanner.metadata(drv))
    check("metadata covers every photo", len(meta) == 313, str(len(meta)))
    check("chat names are carried",
          {m["chat"] for m in meta} == {"chat3", "chat7"},
          str({m["chat"] for m in meta}))


def test_paging_beyond_one_page() -> None:
    print("\n4) a chat with more photos than one page")
    chats = make_chats(1, {0: 250})
    page = FakePage(chats)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    run(scanner.list_chats(drv))
    run(scanner.scan(drv, total_chats=1, slice_size=1))
    meta = run(scanner.metadata(drv))
    check("all 250 photos found, not just the first 100", len(meta) == 250,
          str(len(meta)))
    check("it took more than one search call", page.search_calls >= 3,
          f"{page.search_calls} calls")


# --------------------------------------------------------------------------
# 5. direction filtering and the cap
# --------------------------------------------------------------------------

def test_select() -> None:
    print("\n5) direction filter and cap")
    meta = [{"i": 0, "out": True, "date": 100},
            {"i": 1, "out": False, "date": 200},
            {"i": 2, "out": True, "date": 300},
            {"i": 3, "out": False, "date": 400}]
    check("both keeps everything", len(scanner.select(meta, "both")) == 4)
    sent = scanner.select(meta, "sent")
    check("sent keeps only my photos",
          [m["i"] for m in sent] == [2, 0], str([m["i"] for m in sent]))
    recv = scanner.select(meta, "received")
    check("received keeps only theirs",
          [m["i"] for m in recv] == [3, 1], str([m["i"] for m in recv]))
    check("newest comes first", scanner.select(meta, "both")[0]["date"] == 400)
    capped = scanner.select(meta, "both", limit=2)
    check("the cap keeps the newest", [m["date"] for m in capped] == [400, 300],
          str([m["date"] for m in capped]))


# --------------------------------------------------------------------------
# 6. fetching in batches
# --------------------------------------------------------------------------

def test_fetch() -> None:
    print("\n6) downloading in batches")
    chats = make_chats(2, {0: 50})
    page = FakePage(chats)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    run(scanner.list_chats(drv))
    run(scanner.scan(drv, total_chats=2, slice_size=2))

    prog: list[tuple[int, int]] = []

    async def on_progress(done, total):
        prog.append((done, total))

    res = run(fetcher.fetch(drv, list(range(50)), batch=10,
                            on_progress=on_progress))
    check("fetch reports ok", res.get("ok") is True, str(res.get("code")))
    imgs = [i for i in (res.get("images") or []) if i]
    check("every image came back", len(imgs) == 50, str(len(imgs)))
    check("bytes were decoded from base64",
          all(isinstance(i["bytes"], bytes) and i["bytes"] for i in imgs))
    check("it really batched", page.fetch_calls == 5,
          f"{page.fetch_calls} calls for 50 photos at batch=10")
    check("progress was reported", len(prog) == 5, str(prog))


def test_fetch_stop() -> None:
    print("\n7) stop mid-download")
    chats = make_chats(1, {0: 100})
    page = FakePage(chats)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    run(scanner.list_chats(drv))
    run(scanner.scan(drv, total_chats=1, slice_size=1))
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 2

    res = run(fetcher.fetch(drv, list(range(100)), batch=10,
                            should_stop=should_stop))
    check("stop is honoured", res.get("stopped") is True, str(res.get("stopped")))
    got = len([i for i in (res.get("images") or []) if i])
    check("only what was fetched is kept", 0 < got < 100, str(got))


# --------------------------------------------------------------------------
# 8. the PDF: one page per photo
# --------------------------------------------------------------------------

def _chromium_available() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    if not callable(getattr(sys.modules["playwright.async_api"],
                            "async_playwright", None)):
        return False

    async def _try():
        from playwright.async_api import async_playwright
        try:
            async with async_playwright() as pw:
                b = await pw.chromium.launch(headless=True)
                await b.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    try:
        return run(_try())
    except Exception:  # noqa: BLE001
        return False


def test_pdf() -> None:
    print("\n8) the PDF, one photo per page")
    if not _chromium_available():
        print("      SKIPPED: headless Chromium is not available here")
        return
    from pathlib import Path
    items = [{"bytes": _JPEG_1PX, "chat": f"chat{i}", "when": "2026-01-01 10:00",
              "out": i % 2 == 0} for i in range(7)]
    out = Path(os.environ["ARTIFACTS_DIR"]) / "pdftest"
    res = run(renderer.render(items, out, base_name="t", per_file=100))
    check("render succeeded", res.get("ok") is True, str(res.get("code")))
    files = res.get("files") or []
    check("one file for 7 photos", len(files) == 1, str(len(files)))
    if files:
        import re
        raw = Path(files[0]["path"]).read_bytes()
        pages = len(re.findall(rb"/Type\s*/Page[^s]", raw))
        check("the PDF has one page per photo", pages == 7, f"{pages} pages")
        check("pages recorded in the result", files[0]["pages"] == 7)

    res2 = run(renderer.render(items, out, base_name="split", per_file=3))
    check("a big export splits into files",
          len(res2.get("files") or []) == 3,
          str(len(res2.get("files") or [])))


# --------------------------------------------------------------------------
# 9. the whole engine end to end
# --------------------------------------------------------------------------

def test_engine() -> None:
    print("\n9) the engine end to end")
    chats = make_chats(30, {2: 10, 5: 6, 11: 4})
    page = FakePage(chats)
    drv = FakeDriver(page)
    live = FakeLive()
    reports: list[str] = []
    sent: list[str] = []

    async def report(text):
        reports.append(text)

    async def send_document(path, caption=""):
        sent.append(path)
        return object()

    have_pdf = _chromium_available()

    res = run(engine.export(drv, "989124089268", "989124089268",
                            direction="both", report=report, live=live,
                            send_document=send_document if have_pdf else None))
    if not have_pdf:
        # Without a real Chromium the render must fail GRACEFULLY: a code, no
        # traceback, and the scan/download work still reported.
        check("a missing renderer degrades to an error code, not a crash",
              res.get("ok") is False and bool(res.get("code")),
              str(res.get("code")))
        check("the live card still ran", len(live.texts) > 3, str(len(live.texts)))
        print("      (PDF/delivery not checked: no headless Chromium here)")
        return

    check("export ok", res.get("ok") is True, str(res.get("code")))
    check("20 photos exported", res.get("photos") == 20, str(res.get("photos")))
    check("chats counted", res.get("chats_total") == 30, str(res.get("chats_total")))
    check("chats with photos counted", res.get("chats_with_photos") == 3,
          str(res.get("chats_with_photos")))
    check("direction split adds up",
          (res.get("sent_by_me") or 0) + (res.get("received") or 0) == 20,
          f"{res.get('sent_by_me')} + {res.get('received')}")
    check("a file was produced", len(res.get("files") or []) >= 1)
    check("the file was delivered", len(sent) == len(res.get("files") or []),
          f"{len(sent)} sent")
    check("the live card was painted", len(live.texts) > 3, str(len(live.texts)))
    last = live.texts[-1]
    check("the final card says DONE", "DONE" in last)


def test_engine_nothing_found() -> None:
    print("\n10) an account with no photos")
    chats = make_chats(12, {})
    page = FakePage(chats)
    drv = FakeDriver(page)

    async def report(text):
        return None

    res = run(engine.export(drv, "acc", "989000000000", direction="both",
                            report=report, live=FakeLive()))
    check("it finishes cleanly", res.get("ok") is True, str(res.get("code")))
    check("it says nothing was found", res.get("nothing_found") is True,
          str(res))
    check("chats were still counted", res.get("chats_total") == 12,
          str(res.get("chats_total")))


def test_engine_direction() -> None:
    print("\n11) the direction filter through the engine")
    chats = make_chats(4, {1: 10})       # out alternates, so 5 each way
    for direction, expect in (("sent", 5), ("received", 5), ("both", 10)):
        page = FakePage(chats)
        drv = FakeDriver(page)

        async def report(text):
            return None

        res = run(engine.export(drv, "acc", "9890", direction=direction,
                                report=report, live=FakeLive()))
        if not _chromium_available():
            print(f"      SKIPPED {direction}: no headless Chromium")
            continue
        check(f"{direction} exported {expect}", res.get("photos") == expect,
              f"{res.get('photos')} (code={res.get('code')})")


def main() -> int:
    print("=" * 68)
    print("PHOTO EXPORT")
    print("=" * 68)
    try:
        test_cards()
        test_dialog_paging()
        test_scan()
        test_paging_beyond_one_page()
        test_select()
        test_fetch()
        test_fetch_stop()
        test_pdf()
        test_engine()
        test_engine_nothing_found()
        test_engine_direction()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL PHOTO EXPORT TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
