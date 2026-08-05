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
# The engine reads its pacing from config at import time and the real defaults
# are seconds long on purpose. Zero them so the suite is fast AND deterministic;
# pacing itself is tested directly, with explicit delays.
os.environ["MKWL_PHOTO_DELAY"] = "0"
os.environ["MKWL_PHOTO_SCAN_DELAY"] = "0"


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
                 page_size: int = 100, search_limit: int = 100,
                 flood_after: int | None = None, flood_rounds: int = 0,
                 fail_indexes: set | None = None) -> None:
        self.chats = chats
        self.first_page = first_page
        self.page_size = page_size
        self.search_limit = search_limit
        self.flood_after = flood_after
        self.flood_rounds = flood_rounds
        self.floods_served = 0
        self.served = 0
        self.fail_indexes = fail_indexes or set()
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
        """Answer in the bridge's real shape: one aligned row per index.

        `flood_after` makes the fake behave like a rate-limited account: the
        first N downloads succeed, then FLOOD_WAIT until `flood_rounds` pauses
        have been served, after which it recovers. That is what the live account
        did, and the first version of the fetcher gave up on it.
        """
        self.fetch_calls += 1
        idx = arg.get("idx") or []
        b64 = base64.b64encode(_JPEG_1PX).decode("ascii")
        rows = []
        floods = 0
        for i in idx:
            limited = (self.flood_after is not None
                       and self.served >= self.flood_after
                       and self.floods_served < self.flood_rounds)
            if limited:
                floods += 1
                self.floods_served += 1
                rows.append({"i": i, "ok": False, "code": "FLOOD_WAIT_1",
                             "flood": True, "wait": 1})
                break                      # the bridge stops the call on a flood
            if i in self.fail_indexes:
                rows.append({"i": i, "ok": False, "code": "empty",
                             "flood": False, "wait": 0})
                continue
            self.served += 1
            rows.append({"i": i, "ok": True, "b64": b64,
                         "bytes": len(_JPEG_1PX), "w": 296, "h": 320})
        return {"ok": True, "results": rows,
                "failed": sum(1 for r in rows if not r.get("ok")),
                "floods": floods,
                "wait": 1 if floods else 0}


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

def test_only_private_chats() -> None:
    print("\n2b) channels, groups, bots and Saved Messages are NOT scanned")
    # This is the question the owner keeps asking, so it is pinned by a test that
    # reads the SHIPPED bridge rather than trusting a claim.
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "photo_export" / "bridge.js"
           ).read_text(encoding="utf-8")
    # Split on the DEFINITION, not the name: the name also appears in the header
    # comment, and splitting there left only the comment to inspect.
    marker = "window.__MKWL_px_scan = async function"
    check("the scan function is where expected", marker in src)
    head = src.split(marker, 1)[0]                 # the dialog walk only
    check("the dialog walk keeps peerUser and nothing else",
          "if (p._ !== 'peerUser') continue;" in head)
    check("bots, the self chat and deleted users are dropped",
          "if (f.self || f.bot || f.deleted) continue;" in head)
    # peerChannel/peerChat appear ONLY while rebuilding the paging offset.
    for kind in ("peerChannel", "peerChat"):
        seg = head.split(kind, 1)[1][:120] if kind in head else ""
        check(f"{kind} is used only for offset_peer, never collected",
              "offset_peer" in seg, seg[:60])
    check("peers.push happens after the peerUser guard",
          head.index("if (p._ !== 'peerUser') continue;")
          < head.index("peers.push("))


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
    imgs = res.get("images") or {}
    check("every image came back", len(imgs) == 50, str(len(imgs)))
    check("results are keyed by the original index",
          set(imgs.keys()) == set(range(50)))
    check("bytes were decoded from base64",
          all(isinstance(v["bytes"], bytes) and v["bytes"] for v in imgs.values()))
    check("it really batched", page.fetch_calls == 5,
          f"{page.fetch_calls} calls for 50 photos at batch=10")
    check("progress was reported", len(prog) == 5, str(prog))


def test_fetch_flood_recovers() -> None:
    print("\n6b) a rate limit is a pause, not the end")
    chats = make_chats(1, {0: 40})
    # 10 succeed, then three FLOOD_WAIT_1 answers, then it recovers.
    page = FakePage(chats, flood_after=10, flood_rounds=3)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    run(scanner.list_chats(drv))
    run(scanner.scan(drv, total_chats=1, slice_size=1))

    waits: list[tuple[int, int]] = []

    async def on_wait(seconds, new_conc):
        waits.append((seconds, new_conc))

    res = run(fetcher.fetch(drv, list(range(40)), batch=10, conc=8,
                            on_wait=on_wait))
    check("fetch still reports ok", res.get("ok") is True, str(res.get("code")))
    check("it did NOT give up", res.get("gave_up") is False, str(res.get("gave_up")))
    check("every photo arrived after the pauses",
          len(res.get("images") or {}) == 40,
          f"{len(res.get('images') or {})} of 40")
    check("it waited at least once", len(waits) >= 1, str(waits))
    check("it waited the seconds the server named",
          all(w[0] == 1 for w in waits), str(waits))
    check("concurrency was reduced", res.get("conc_final", 8) < 8,
          f"ended at {res.get('conc_final')}")
    check("the wait was accounted for", (res.get("waited") or 0) >= 1,
          str(res.get("waited")))


def test_fetch_gives_up_loudly() -> None:
    print("\n6c) a server that never relents -> partial, never a fake DONE")
    chats = make_chats(1, {0: 40})
    page = FakePage(chats, flood_after=5, flood_rounds=10_000)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    run(scanner.list_chats(drv))
    run(scanner.scan(drv, total_chats=1, slice_size=1))
    res = run(fetcher.fetch(drv, list(range(40)), batch=10, conc=8))
    check("it reports ok with a partial result", res.get("ok") is True)
    check("gave_up is set", res.get("gave_up") is True, str(res.get("gave_up")))
    got = len(res.get("images") or {})
    check("what was fetched is kept", 0 < got < 40, f"{got} of 40")
    check("the remainder is counted", (res.get("missing") or 0) > 0,
          str(res.get("missing")))


def test_fetch_stall_guard() -> None:
    print("\n6d) a bridge answering nonsense must not spin forever")

    class MutePage(FakePage):
        def _fetch(self, arg):
            self.fetch_calls += 1
            return {"ok": True, "results": [], "failed": 0, "floods": 0,
                    "wait": 0}

    chats = make_chats(1, {0: 10})
    page = MutePage(chats)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    run(scanner.list_chats(drv))
    run(scanner.scan(drv, total_chats=1, slice_size=1))
    res = run(fetcher.fetch(drv, list(range(10)), batch=5))
    check("it bails out instead of looping", res.get("ok") is False,
          str(res.get("ok")))
    check("the reason is explicit", res.get("code") == "fetch_stalled",
          str(res.get("code")))
    check("it gave up quickly", page.fetch_calls <= 5,
          f"{page.fetch_calls} calls")


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
    got = len(res.get("images") or {})
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

def test_pacing() -> None:
    print("\n7c) deliberate pacing between batches and slices")
    import time as _time

    # -- the scan pauses between slices -------------------------------
    chats = make_chats(12, {})
    page = FakePage(chats)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    run(scanner.list_chats(drv))
    t0 = _time.time()
    res = run(scanner.scan(drv, total_chats=12, slice_size=4, delay=0.05))
    slow = _time.time() - t0
    check("the scan still completes", res.get("scanned") == 12,
          str(res.get("scanned")))
    # 3 slices -> 2 pauses.
    check("the scan actually paused between slices", slow >= 0.09,
          f"{slow:.3f}s for 2 pauses of 0.05s")

    t0 = _time.time()
    run(scanner.scan(drv, total_chats=12, slice_size=4, delay=0))
    fast = _time.time() - t0
    check("delay=0 does not pause", fast < slow, f"{fast:.3f}s vs {slow:.3f}s")

    # -- the download pauses between batches --------------------------
    chats = make_chats(1, {0: 30})
    page = FakePage(chats)
    drv = FakeDriver(page)
    run(scanner.ensure_bridge(drv))
    run(scanner.list_chats(drv))
    run(scanner.scan(drv, total_chats=1, slice_size=1))

    paces: list[float] = []

    async def on_pace(seconds):
        paces.append(seconds)

    t0 = _time.time()
    res = run(fetcher.fetch(drv, list(range(30)), batch=10, conc=3,
                            delay=0.05, on_pace=on_pace))
    took = _time.time() - t0
    check("every photo still arrives", len(res.get("images") or {}) == 30,
          str(len(res.get("images") or {})))
    # 3 batches -> 2 pauses.
    check("it paused between batches", took >= 0.09,
          f"{took:.3f}s for 2 pauses of 0.05s")
    check("the card was told about the pause", len(paces) == 2, str(paces))
    check("the pause is accounted for", (res.get("paced") or 0) > 0,
          str(res.get("paced")))

    t0 = _time.time()
    run(fetcher.fetch(drv, list(range(30)), batch=10, conc=3, delay=0))
    quick = _time.time() - t0
    check("delay=0 downloads without pausing", quick < took,
          f"{quick:.3f}s vs {took:.3f}s")


def test_partial_card() -> None:
    print("\n7b) the result card must not fake a DONE")
    short = px_cards.finished(
        account="989213725238", phone="989213725238", direction="both",
        photos=15, sent_by_me=7, received=8, chats_with_photos=171,
        chats_total=429, files=[{"name": "a.pdf", "pages": 15, "kb": 191}],
        elapsed=67.0, skipped=485, partial=True, requested=500,
        photos_available=4853, rate_limited=True, waited=12,
        note="Eitaa kept rate-limiting")
    check("status says PARTIAL, not DONE", "\u2022 Status : PARTIAL" in short)
    check("the title is not a green tick", "\u2705" not in short)
    check("the bar is NOT full",
          "[" + "\u2588" * 10 + "]" not in short.split("Exported")[1][:40],
          short.split("Exported")[1][:40])
    check("it shows what was asked for", "Photos requested : 500" in short)
    check("it shows what exists", "Photos in chats : 4,853" in short)
    check("it names the shortfall", "Not downloaded : 485" in short)
    check("it reports the waiting", "Waited for limits : 12s" in short)
    check("the footer tells the owner what to do",
          "again" in short.lower())

    named = px_cards.finished(
        account="a", phone="9890", direction="both", photos=30, sent_by_me=15,
        received=15, chats_with_photos=3, chats_total=40,
        files=[{"name": "a.pdf", "pages": 30, "kb": 90}], elapsed=9.0,
        requested=30,
        top_chats=[("F. Bahadoran", 18), ("\u0645\u0633\u0639\u0648\u062f", 9),
                   ("", 3)])
    check("the card names the chats the photos came from",
          "Top chats (private only)" in named)
    check("a chat name is listed with its count",
          "F. Bahadoran - 18" in named, named)
    check("an unnamed chat is labelled, not blank",
          "(no name)" in named)

    full = px_cards.finished(
        account="a", phone="9890", direction="both", photos=20, sent_by_me=10,
        received=10, chats_with_photos=3, chats_total=30,
        files=[{"name": "a.pdf", "pages": 20, "kb": 50}], elapsed=5.0,
        skipped=0, partial=False, requested=20)
    check("a complete run does say DONE", "\u2022 Status : DONE" in full)
    check("a complete run gets the tick", "\u2705" in full)
    check("a complete run fills the bar",
          "[" + "\u2588" * 10 + "]" in full)


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
        test_only_private_chats()
        test_scan()
        test_paging_beyond_one_page()
        test_select()
        test_fetch()
        test_fetch_flood_recovers()
        test_fetch_gives_up_loudly()
        test_fetch_stall_guard()
        test_fetch_stop()
        test_pacing()
        test_partial_card()
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
