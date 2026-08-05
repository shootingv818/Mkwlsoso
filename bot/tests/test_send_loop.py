"""End-to-end tests for the send loop with a FAKE driver (no browser, no network).

Run: python -m bot.tests.test_send_loop

The send loop is where every expensive bug lived, so it is exercised here against
a fake Eitaa: concurrency, resume, the lost-upload rebuild, server-declared
waits, the failure brake, stop, and the zero-recipient guard.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_send_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")


def _stub_playwright() -> None:
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


_stub_playwright()

from bot import contacts_store, progress_store  # noqa: E402
from bot import runner as R  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name} {extra}")


# ---- fakes -------------------------------------------------------------

class FakeDriver:
    """Stands in for EitaaDriver. Records calls and can be told to misbehave."""

    def __init__(self, session=None, *, logged_in=True, fail_peers=(),
                 lose_upload_at=None, flood_at=None, flood_code="FLOOD_WAIT_1",
                 init_ok=True, ui_ok=True, track_inflight=None):
        self.logged_in = logged_in
        self.fail_peers = set(fail_peers)
        self.lose_upload_at = lose_upload_at
        self.flood_at = flood_at
        self.flood_code = flood_code
        self.init_ok = init_ok
        self.ui_ok = ui_ok
        self.calls = {"init": 0, "bridge": 0, "ui": 0}
        self.collected = []          # what a fresh contacts collection returns
        self.upload_alive = True
        self._lost_once = False
        self.peak_inflight = 0
        self._inflight = 0
        self._track = track_inflight

    # --- lifecycle ---
    async def open(self):
        return None

    async def is_logged_in(self):
        return self.logged_in

    async def ensure_bridge(self):
        return True

    async def bridge_harvest_peers(self, peer_ids):
        return {"ok": False}

    async def _return_to_chat_list(self):
        return None

    # --- file path ---
    async def bridge_file_init(self, path, caption="", locate_timeout=None):
        self.calls["init"] += 1
        if not self.init_ok:
            return {"ok": False, "code": "locate_failed (test)"}
        self.upload_alive = True
        if self.calls["init"] > 1:
            # A REBUILT upload stays alive; only the first one gets dropped.
            self._lost_once = True
        return {"ok": True, "msg_id": 100 + self.calls["init"]}

    async def collect_all_contacts(self, max_scrolls=120, should_stop=None):
        return list(self.collected)

    async def bridge_contacts_list(self):
        return {"ok": True, "count": len(self.collected),
                "contacts": list(self.collected), "skipped": 0,
                "raw": len(self.collected)}

    async def bridge_file_ready(self):
        return self.upload_alive

    async def bridge_file_send(self, peer_id, caption=""):
        return await self._send(peer_id)

    async def bridge_send(self, peer_id, text):
        return await self._send(peer_id)

    async def _send(self, peer_id):
        self.calls["bridge"] += 1
        # Captured BEFORE the await: with concurrency every call would otherwise
        # read the same post-increment total and "the 2nd call" would never match.
        n = self.calls["bridge"]
        self._inflight += 1
        self.peak_inflight = max(self.peak_inflight, self._inflight)
        try:
            await asyncio.sleep(0.01)  # so overlapping calls really overlap
            if self.flood_at is not None and n == self.flood_at:
                return {"ok": False, "limit": True, "code": self.flood_code}
            if (self.lose_upload_at is not None and n >= self.lose_upload_at
                    and self.upload_alive and not self._lost_once):
                # Simulate the page dropping the upload state exactly once.
                self.upload_alive = False
            if not self.upload_alive:
                return {"ok": False, "code": "file not initialized"}
            if str(peer_id) in self.fail_peers:
                return {"ok": False, "code": "PEER_ID_INVALID"}
            return {"ok": True, "method": "sendMedia", "msg_id": 500 + n}
        finally:
            self._inflight -= 1

    # --- UI fallback ---
    async def send_file(self, path, caption="", query=""):
        self.calls["ui"] += 1
        await asyncio.sleep(0.01)
        return R.SendResult(ok=self.ui_ok, to=query, detail="ui" if self.ui_ok else "ui failed")

    async def send_text(self, name, text, verify=True):
        self.calls["ui"] += 1
        await asyncio.sleep(0.01)
        return R.SendResult(ok=self.ui_ok, to=name, detail="ui" if self.ui_ok else "ui failed")


class _FakePage:
    """Minimal page: the send loop may reload it after a failed upload."""

    def __init__(self):
        self.reloads = 0

    async def reload(self, **kw):
        self.reloads += 1

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)


class _FakeSessionCtx:
    async def __aenter__(self):
        return types.SimpleNamespace(page=_FakePage())

    async def __aexit__(self, *a):
        return False


class _FakePool:
    """Stands in for the standby session pool; counts how many leases happened."""

    def __init__(self):
        self.leases = 0

    def lease(self, account, **kw):
        self.leases += 1
        return _FakeSessionCtx()


def install_driver(driver: FakeDriver, pool: "_FakePool | None" = None):
    """Point the runner at the fake driver/session/pool for one test."""
    R.session_pool = pool or _FakePool()
    R.open_session = lambda account, **kw: _FakeSessionCtx()
    R.EitaaDriver = lambda session: driver
    return R.session_pool


async def run_send(driver: FakeDriver, *, account="acc", contacts=None,
                   content=None, settings=None, stop_after=None):
    install_driver(driver)
    if contacts is not None:
        contacts_store.save(account, contacts)
    content = content or {"kind": "file", "file_path": __file__,
                          "file_name": "t.py", "caption": "c"}
    settings = settings or {"text_send_delay": 0, "send_log_every": 1000,
                            "send_concurrency": 1}
    mgr = R.JobManager()
    job = R.Job(job_id="test", kind="send", account=account)
    lines: list[str] = []

    async def report(text):
        lines.append(text)

    task = asyncio.create_task(
        mgr._send_job(job, content, settings, report, None))
    if stop_after is not None:
        await asyncio.sleep(stop_after)
        job.ask_stop()
    await task
    return job, lines, driver


async def run_send_force_cancelled(driver: FakeDriver, *, account, contacts,
                                   content, settings, cancel_after=0.05,
                                   cancels=2):
    """Start a send and CANCEL the task, like Force Stop does.

    The existing helper only ever calls `ask_stop()`, which is the graceful first
    press. The second press cancels the task, and a third press cancels it AGAIN --
    that second cancellation is what used to lose the ledger, because the flush sat
    after an `await` inside the `finally` and `except Exception` cannot catch a
    CancelledError.
    """
    install_driver(driver)
    contacts_store.save(account, contacts)
    mgr = R.JobManager()
    job = R.Job(job_id="force", kind="send", account=account)
    lines: list[str] = []

    async def report(text):
        lines.append(text)

    task = asyncio.create_task(mgr._send_job(job, content, settings, report, None))
    await asyncio.sleep(cancel_after)
    for _ in range(max(1, cancels)):
        task.cancel()
        await asyncio.sleep(0)
    try:
        await task
    except asyncio.CancelledError:
        pass
    return job, lines, driver


def peers(n, start=1000):
    return [{"peer_id": str(start + i), "title": f"n{i}", "access_hash": "h"}
            for i in range(n)]


# ---- tests -------------------------------------------------------------

def test_sequential_baseline():
    print("sequential baseline (concurrency=1)")
    progress_store.clear("a_seq")
    d = FakeDriver()
    job, lines, d = asyncio.run(run_send(d, account='a_seq', contacts=peers(10)))
    check("everyone got it", job.summary.get("sent") == 10, job.summary)
    check("no failures", job.summary.get("failed") == 0)
    check("file uploaded exactly once", d.calls["init"] == 1, d.calls)
    check("no UI fallback used", d.calls["ui"] == 0, d.calls)
    check("one at a time", d.peak_inflight == 1, d.peak_inflight)
    check("ledger recorded all", progress_store.done_count(
        "a_seq", progress_store.content_key({"kind": "file", "file_path": __file__,
                                           "file_name": "t.py", "caption": "c"})) == 10)


def test_concurrency_overlaps():
    print("concurrency actually overlaps")
    progress_store.clear("a_conc")
    d = FakeDriver()
    job, _, d = asyncio.run(run_send(
        d, account='a_conc', contacts=peers(12),
        settings={"text_send_delay": 0, "send_log_every": 1000, "send_concurrency": 4}))
    check("all delivered", job.summary.get("sent") == 12, job.summary)
    check("ran 4 in flight", d.peak_inflight == 4, d.peak_inflight)
    check("still uploaded once", d.calls["init"] == 1, d.calls)
    check("summary records concurrency", job.summary.get("concurrency") == 4)


def test_concurrency_is_faster():
    print("concurrency is measurably faster")
    progress_store.clear("a_fast1")
    progress_store.clear("a_fast5")
    d1 = FakeDriver()
    t0 = asyncio.get_event_loop_policy().new_event_loop().close()  # noqa: F841
    import time as _t
    s = _t.perf_counter()
    asyncio.run(run_send(d1, account="a_fast1", contacts=peers(20),
                         settings={"text_send_delay": 0.02, "send_log_every": 999,
                                   "send_concurrency": 1}))
    seq = _t.perf_counter() - s
    d2 = FakeDriver()
    s = _t.perf_counter()
    asyncio.run(run_send(d2, account="a_fast5", contacts=peers(20),
                         settings={"text_send_delay": 0.02, "send_log_every": 999,
                                   "send_concurrency": 5}))
    par = _t.perf_counter() - s
    check(f"5-at-a-time beats sequential ({seq*1000:.0f}ms -> {par*1000:.0f}ms)",
          par < seq * 0.8, (seq, par))


def test_lost_upload_is_rebuilt():
    print("lost upload is rebuilt, not re-uploaded per recipient")
    progress_store.clear("a_lost")
    d = FakeDriver(lose_upload_at=3)
    job, _, d = asyncio.run(run_send(d, account='a_lost', contacts=peers(8)))
    check("all delivered anyway", job.summary.get("sent") == 8, job.summary)
    check("re-init happened exactly once", job.summary.get("reinits") == 1, job.summary)
    check("re-init stayed bounded",
          job.summary["reinits"] <= R._MAX_FILE_REINITS, job.summary)
    check("no per-recipient re-upload", d.calls["ui"] == 0, d.calls)
    check("file uploaded twice total (initial + rebuild)", d.calls["init"] == 2, d.calls)


def test_dead_upload_stops_instead_of_crawling():
    print("an unrecoverable upload stops the run instead of crawling")
    progress_store.clear("a_dead")
    # init works, but every reuse fails: the worst case that produced 25s per
    # recipient on the live bot.
    d = FakeDriver(lose_upload_at=1)
    d._lost_once = False

    async def always_lost(peer_id, caption=""):
        d.calls["bridge"] += 1
        return {"ok": False, "code": "file not initialized"}

    d.bridge_file_send = always_lost
    job, lines, d = asyncio.run(run_send(d, account='a_dead', contacts=peers(60)))
    check("stopped early", job.summary.get("sent", 0) <= R._MAX_UI_FILE_FALLBACKS + 1,
          job.summary)
    check("explained the stop",
          any("SLOW PATH" in x.upper() for x in lines), lines[-1:])
    check("progress was saved for the resume",
          progress_store.done_count("a_dead", progress_store.content_key(
              {"kind": "file", "file_path": __file__, "file_name": "t.py",
               "caption": "c"})) == job.summary.get("sent"), job.summary)


def test_short_flood_wait_is_obeyed():
    print("short server wait is obeyed, run continues")
    progress_store.clear("a_flood_s")
    d = FakeDriver(flood_at=2, flood_code="FLOOD_WAIT_1")
    job, lines, d = asyncio.run(run_send(d, account='a_flood_s', contacts=peers(5)))
    check("run was not abandoned", job.summary.get("sent") == 5, job.summary)
    check("no restriction card for a short wait",
          not any("RESTRICT" in x.upper() for x in lines), lines[:2])


def test_peer_flood_can_be_configured_to_continue():
    print("PEER_FLOOD: pause by default, keep going when the owner says so")
    base = {"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 1}

    # Default: the run pauses on the restriction.
    progress_store.clear("a_pf_stop")
    d = FakeDriver(flood_at=2, flood_code="PEER_FLOOD")
    job, lines, d = asyncio.run(run_send(
        d, account="a_pf_stop", contacts=peers(10),
        content={"kind": "text", "text": "pf"}, settings=dict(base)))
    check("paused by default", job.summary.get("sent", 0) < 10, job.summary)
    check("said it auto-paused",
          any("auto-paused" in x for x in lines), lines[-2:])
    check("explained PEER_FLOOD is not a wait",
          any("not a timed wait" in x for x in lines), lines[-2:])

    # Owner turned the pause off: every recipient is attempted, restrictions are
    # reported (capped) and only Stop ends the run.
    progress_store.clear("a_pf_go")

    class AlwaysFlood(FakeDriver):
        async def _send(self, peer_id):
            self.calls["bridge"] += 1
            await asyncio.sleep(0)
            if self.calls["bridge"] % 2 == 0:
                return {"ok": False, "limit": True, "code": "PEER_FLOOD"}
            return {"ok": True, "method": "sendText", "msg_id": self.calls["bridge"]}

    d2 = AlwaysFlood()
    job2, lines2, d2 = asyncio.run(run_send(
        d2, account="a_pf_go", contacts=peers(10),
        content={"kind": "text", "text": "pf2"},
        settings=dict(base, stop_on_limit=False)))
    check("every recipient was attempted", d2.calls["bridge"] == 10, d2.calls)
    check("the good half was delivered", job2.summary.get("sent") == 5, job2.summary)
    check("the refused half counted as failed", job2.summary.get("failed") == 5,
          job2.summary)
    cards_posted = sum(1 for x in lines2 if "LIMIT DETECTED" in x)
    check(f"restriction cards are capped ({cards_posted})", 1 <= cards_posted <= 3,
          cards_posted)
    check("the card says the run continues",
          any("pause on limit is off" in x.lower() for x in lines2), lines2[:3])


def test_long_flood_wait_stops():
    print("long server wait stops the run")
    progress_store.clear("a_flood_l")
    d = FakeDriver(flood_at=2, flood_code="FLOOD_WAIT_9999")
    job, lines, d = asyncio.run(run_send(d, account='a_flood_l', contacts=peers(5)))
    check("stopped early", job.summary.get("sent", 0) < 5, job.summary)
    check("a limit card was posted",
          any("LIMIT" in x.upper() or "RESTRICT" in x.upper() for x in lines), lines[:3])


def test_force_stop_TWICE_keeps_the_ledger():
    print("force stop pressed AGAIN still records what was delivered")
    # The existing force-stop test cancels ONCE, and a single cancellation always
    # survived. Pressing Force Stop a second time cancels the task again, which
    # raises CancelledError at the first await inside the `finally` -- and
    # `except Exception` cannot catch it, so everything after that await was
    # skipped, including the ledger write.
    acct = "a_force"
    progress_store.clear(acct)
    content = {"kind": "text", "text": "force stop ledger"}
    key = progress_store.content_key(content)
    settings = {"text_send_delay": 0.01, "send_log_every": 1000,
                "send_concurrency": 1}
    d = FakeDriver()
    job, _, d = asyncio.run(run_send_force_cancelled(
        d, account=acct, contacts=peers(40), content=content,
        settings=settings, cancel_after=0.12, cancels=2))

    on_disk = progress_store.done_count(acct, key)
    delivered = d.calls["bridge"]
    check("some recipients were reached before the cancel", delivered > 0,
          f"{delivered} sends")
    # THE BUG: with the flush after an await in the finally, a second cancel threw
    # the record away and this was 0 while messages had already gone out.
    check("the ledger was written despite the double cancel", on_disk > 0,
          f"{on_disk} recorded, {delivered} actually sent")
    check("it recorded no more than it sent", on_disk <= delivered,
          f"{on_disk} recorded vs {delivered} sent")

    # And the resume must now skip exactly those people.
    d2 = FakeDriver()
    job2, lines2, d2 = asyncio.run(run_send(
        d2, account=acct, content=content, settings=settings))
    total = job2.summary.get("sent", 0) + on_disk
    check("nobody is messaged twice and nobody is missed", total == 40,
          f"{on_disk} before + {job2.summary.get('sent', 0)} after = {total}")


def test_ledger_flush_batch_is_small():
    print("the ledger batch is small enough that a kill loses little")
    acct = "a_batch"
    progress_store.clear(acct)
    key = progress_store.content_key({"kind": "text", "text": "batch"})
    led = progress_store.open_ledger(acct, key)
    # Marking `FLUSH_EVERY` people must reach the disk without any explicit flush.
    for i in range(progress_store.FLUSH_EVERY):
        led.mark(f"n{i}", str(2000 + i))
    check(f"a batch of {progress_store.FLUSH_EVERY} auto-flushes",
          progress_store.done_count(acct, key) == progress_store.FLUSH_EVERY,
          f"{progress_store.done_count(acct, key)} on disk")
    check("the batch is at most 5 people", progress_store.FLUSH_EVERY <= 5,
          str(progress_store.FLUSH_EVERY))


def test_resume_skips_delivered():
    print("resume skips who already got it")
    progress_store.clear("a_resume")
    content = {"kind": "text", "text": "hello"}
    settings = {"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 1}
    d = FakeDriver()
    job, _, _ = asyncio.run(run_send(d, account='a_resume', contacts=peers(6), content=content,
                                     settings=settings))
    check("first run delivered all", job.summary.get("sent") == 6, job.summary)
    d2 = FakeDriver()
    job2, lines2, d2 = asyncio.run(run_send(d2, account='a_resume', content=content, settings=settings))
    check("second run sends nothing", d2.calls["bridge"] == 0, d2.calls)
    check("and says so", any("NOTHING LEFT" in x.upper() for x in lines2), lines2[:2])
    d3 = FakeDriver()
    job3, _, d3 = asyncio.run(run_send(
        d3, account='a_resume', content={"kind": "text", "text": "a different message"}, settings=settings))
    check("changed content goes to everyone again", job3.summary.get("sent") == 6,
          job3.summary)


def test_partial_resume():
    print("a stopped run resumes from where it stopped")
    progress_store.clear("a_partial")
    content = {"kind": "text", "text": "partial"}
    key = progress_store.content_key(content)
    led = progress_store.open_ledger("a_partial", key)
    for i in range(4):
        led.mark(f"n{i}", str(1000 + i))
    led.flush()
    d = FakeDriver()
    job, lines, d = asyncio.run(run_send(
        d, account='a_partial', contacts=peers(10), content=content,
        settings={"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 2}))
    check("only the remainder was sent", d.calls["bridge"] == 6, d.calls)
    check("resume was announced", any("RESUMING" in x.upper() for x in lines), lines[:2])
    check("ledger now complete", progress_store.done_count("a_partial", key) == 10)


def test_zero_recipients_uploads_nothing():
    print("zero recipients uploads nothing")
    progress_store.clear("empty")
    contacts_store.save("empty", [])
    d = FakeDriver()
    job, lines, d = asyncio.run(run_send(d, account="empty", contacts=[]))
    check("no upload attempted", d.calls["init"] == 0, d.calls)
    check("no send attempted", d.calls["bridge"] == 0, d.calls)
    check("reported as no_recipients",
          any("no_recipients" in x for x in lines), lines[:2])


def test_failure_brake():
    print("failure brake tolerates a rough patch, then stops (text)")
    progress_store.clear("a_brake")
    d = FakeDriver(fail_peers=[str(1000 + i) for i in range(40)], ui_ok=False)
    job, lines, d = asyncio.run(run_send(
        d, account='a_brake', contacts=peers(40),
        content={"kind": "text", "text": "brake"},
        settings={"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 1}))
    failed = job.summary.get("failed", 0)
    check("did not stop at 5 failures", failed > 5, job.summary)
    check("stopped at the brake", failed <= R._FAILURE_BRAKE + 1, job.summary)
    check("a pause card explains it",
          any("PAUSED" in x.upper() or "consecutive" in x for x in lines), lines[:3])


def test_file_stops_early_when_the_browser_path_keeps_failing():
    print("a file job stops fast when every recipient needs the browser path")
    progress_store.clear("a_brake_file")
    # Every peer is rejected by the fast path AND the browser path fails: this is
    # exactly the live case that ground on for an hour and delivered nothing.
    d = FakeDriver(fail_peers=[str(1000 + i) for i in range(40)], ui_ok=False)
    job, lines, d = asyncio.run(run_send(d, account='a_brake_file', contacts=peers(40)))
    check("stopped within the slow-path budget",
          job.summary.get("failed", 0) <= R._MAX_UI_FILE_FALLBACKS + 1, job.summary)
    check("nothing was delivered", job.summary.get("sent") == 0, job.summary)
    check("the reason is on a card",
          any("SLOW PATH" in x.upper() for x in lines), lines[-1:])


def test_upload_failure_stops_instead_of_grinding():
    print("a failed upload stops the run rather than trying the browser 283 times")
    progress_store.clear("a_upfail")

    class NoUpload(FakeDriver):
        async def bridge_file_init(self, path, caption="", locate_timeout=None):
            self.calls["init"] += 1
            return {"ok": False, "code": "locate_failed (upload not found within 139s)"}

    d = NoUpload()
    d.page_reloads = 0
    job, lines, d = asyncio.run(run_send(d, account='a_upfail', contacts=peers(283)))
    check("upload was retried once", d.calls["init"] == 2, d.calls)
    check("no browser uploads were attempted", d.calls["ui"] == 0, d.calls)
    check("nothing was sent", job.summary.get("sent", 0) == 0 or not job.summary,
          job.summary)
    check("the stop is explained",
          any("UPLOAD FAILED" in x.upper() for x in lines), lines[-1:])


def test_ui_fallback_when_no_peer_id():
    print("contacts without peer_id still go through the UI path")
    progress_store.clear("a_ui")
    d = FakeDriver()
    job, _, d = asyncio.run(run_send(d, account='a_ui', contacts=[
        {"title": "only-a-name"}, {"peer_id": "1001", "title": "has-id"}]))
    check("both delivered", job.summary.get("sent") == 2, job.summary)
    check("one used the UI path", d.calls["ui"] == 1, d.calls)
    check("one used the fast path", d.calls["bridge"] == 1, d.calls)
    check("summary splits the paths",
          job.summary.get("via_bridge") == 1 and job.summary.get("via_fallback") == 1,
          job.summary)


def test_stop_is_immediate():
    print("stop takes effect immediately")
    progress_store.clear("a_stop")
    d = FakeDriver()
    job, _, d = asyncio.run(run_send(
        d, account='a_stop', contacts=peers(200),
        settings={"text_send_delay": 5, "send_log_every": 999, "send_concurrency": 1},
        stop_after=0.15))
    check("stopped well before the end", job.summary.get("sent", 0) < 200, job.summary)
    check("progress up to the stop was kept",
          progress_store.done_count("a_stop", progress_store.content_key(
              {"kind": "file", "file_path": __file__, "file_name": "t.py",
               "caption": "c"})) == job.summary.get("sent"), job.summary)


def test_not_logged_in():
    print("a logged-out account fails fast")
    progress_store.clear("a_out")
    d = FakeDriver(logged_in=False)
    job, lines, d = asyncio.run(run_send(d, account='a_out', contacts=peers(5)))
    check("nothing was uploaded", d.calls["init"] == 0, d.calls)
    check("nothing was sent", d.calls["bridge"] == 0, d.calls)
    check("reported not_logged_in", any("not_logged_in" in x for x in lines), lines[:2])


def test_force_stop_keeps_the_ledger():
    print("force stop (task cancel) still records what was delivered")
    progress_store.clear("a_cancel")
    content = {"kind": "text", "text": "cancel me"}
    key = progress_store.content_key(content)

    async def scenario():
        d = FakeDriver()
        install_driver(d)
        contacts_store.save("a_cancel", peers(80))
        mgr = R.JobManager()
        job = R.Job(job_id="t", kind="send", account="a_cancel")
        task = asyncio.create_task(mgr._send_job(
            job, content,
            {"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 1},
            lambda _t: asyncio.sleep(0), None))
        await asyncio.sleep(0.2)
        task.cancel()                     # exactly what Force Stop does
        try:
            await task
        except asyncio.CancelledError:
            pass
        return d

    d = asyncio.run(scenario())
    recorded = progress_store.done_count("a_cancel", key)
    check("some sends happened", d.calls["bridge"] > 0, d.calls)
    check(f"they were recorded ({recorded} of {d.calls['bridge']})",
          recorded >= d.calls["bridge"] - 1, (recorded, d.calls))


def test_exception_does_not_lose_the_ledger():
    print("a driver exception is contained, not fatal")
    progress_store.clear("a_exc")
    content = {"kind": "text", "text": "boom"}

    class Boom(FakeDriver):
        async def bridge_send(self, peer_id, text):
            self.calls["bridge"] += 1
            if self.calls["bridge"] == 3:
                raise RuntimeError("page crashed")
            return {"ok": True, "method": "sendText", "msg_id": 1}

    d = Boom(ui_ok=True)
    job, lines, d = asyncio.run(run_send(
        d, account='a_exc', contacts=peers(6), content=content,
        settings={"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 1}))
    check("the run continued past the crash", job.summary.get("sent", 0) >= 5,
          job.summary)
    check("summary survived", bool(job.summary), job.summary)
    check("ledger has the successes",
          progress_store.done_count("a_exc", progress_store.content_key(content)) >= 5)


def test_batch_results_are_not_discarded_on_limit():
    print("a limit mid-batch does not lose the sends already made")
    progress_store.clear("a_batch")
    content = {"kind": "text", "text": "batchlimit"}
    key = progress_store.content_key(content)
    # 4-wide batch, the 2nd reply is a hard limit; the 3rd and 4th already
    # reached the server before we saw it.
    d = FakeDriver(flood_at=2, flood_code="PEER_FLOOD")
    job, lines, d = asyncio.run(run_send(
        d, account='a_batch', contacts=peers(8), content=content,
        settings={"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 4}))
    delivered = d.calls["bridge"] - 1        # one of the four was the limit reply
    check(f"successes were counted ({job.summary.get('sent')} of {delivered})",
          job.summary.get("sent") == delivered, (job.summary, d.calls))
    check("and recorded, so they are not resent",
          progress_store.done_count("a_batch", key) == job.summary.get("sent"),
          progress_store.done_count("a_batch", key))
    check("the run still stopped", job.summary.get("sent") < 8, job.summary)


def test_truncated_list_cannot_shrink_the_cache():
    print("a truncated scrape cannot overwrite a complete list")
    full = peers(500)
    contacts_store.save("shrink", full)
    check("full list saved", contacts_store.count("shrink") == 500)
    truncated = [{"peer_id": None, "title": f"n{i}"} for i in range(120)]
    contacts_store.save("shrink", truncated)
    check("partial list refused", contacts_store.count("shrink") == 500,
          contacts_store.count("shrink"))
    better = peers(600)
    contacts_store.save("shrink", better)
    check("a bigger real list is accepted", contacts_store.count("shrink") == 600)


def test_ledger_survives_losing_peer_ids():
    print("resume still works when peer_ids change source")
    progress_store.clear("mix")
    content = {"kind": "text", "text": "mix"}
    key = progress_store.content_key(content)
    led = progress_store.open_ledger("mix", key)
    led.mark("Ali", "555")
    led.flush()
    again = progress_store.open_ledger("mix", key)
    check("found by peer_id", again.has("Ali", "555"))
    check("found by name when the id is gone", again.has("Ali", None))
    check("a different person is not matched", not again.has("Reza", None))


class _FakeLive:
    """Captures every card text the job paints, in order."""

    def __init__(self):
        self.texts = []

    async def set(self, text, force=False):
        self.texts.append(text)


def test_hybrid_engine_end_to_end():
    print("hybrid engine drives a real run through the loop")
    progress_store.clear("a_hyb")
    sent_direct = []

    class FakeSender:
        account = "a_hyb"

        def upload_file(self, path, caption=""):
            return {"ok": True}

        def send_text(self, peer, text):
            sent_direct.append(peer)
            # Every third recipient is refused by the server.
            if len(sent_direct) % 3 == 0:
                return {"ok": False, "limit": True, "code": "PEER_FLOOD"}
            return {"ok": True, "method": "direct/sendMessage"}

        def send_uploaded_file(self, peer, caption=""):
            sent_direct.append(peer)
            return {"ok": True, "method": "direct/sendMedia"}

        def close(self):
            return None

    d = FakeDriver()

    async def go():
        install_driver(d)
        contacts_store.save("a_hyb", [
            {"peer_id": str(2000 + i), "access_hash": str(9000 + i), "title": f"h{i}"}
            for i in range(9)])
        # Pretend the direct engine is set up and its context is fresh.
        R.direct_ctx.refresh_from_driver = lambda drv, acc: _ok_refresh()
        R.direct_ctx.has_context = lambda acc: True
        import direct.sender as ds
        ds.DirectSender = lambda account: FakeSender()
        mgr = R.JobManager()
        job = R.Job(job_id="t", kind="send", account="a_hyb")
        lines = []

        async def report(t):
            lines.append(t)

        await mgr._send_job(job, {"kind": "text", "text": "hy"},
                            {"text_send_delay": 0, "send_log_every": 999,
                             "send_concurrency": 3, "engine": "hybrid",
                             "stop_on_limit": False},
                            report, None)
        return job, lines

    async def _ok_refresh():
        return {"ok": True, "kept": 3, "user_id": 1}

    job, lines = asyncio.run(go())
    check("the run used the hybrid engine", job.summary.get("engine") == "hybrid",
          job.summary)
    check("the browser-free engine did the sending", len(sent_direct) == 9,
          len(sent_direct))
    check("deliverable recipients got it", job.summary.get("sent") == 6, job.summary)
    check("refused ones are counted, not retried", job.summary.get("failed") == 3,
          job.summary)
    check("the refusals were remembered",
          R.blocked_store.count("a_hyb") == 3, R.blocked_store.count("a_hyb"))
    check("timing was measured", (job.summary.get("timing") or {}).get("total") is not None,
          job.summary.get("timing"))
    check("a timing card was posted", any("RUN TIMING" in x for x in lines), lines[-1:])

    # Second run: the refused peers must be skipped up front.
    sent_direct.clear()
    progress_store.clear("a_hyb")

    async def go2():
        import direct.sender as ds
        ds.DirectSender = lambda account: FakeSender()
        mgr = R.JobManager()
        job2 = R.Job(job_id="t2", kind="send", account="a_hyb")
        lines2 = []

        async def report(t):
            lines2.append(t)

        await mgr._send_job(job2, {"kind": "text", "text": "hy2"},
                            {"text_send_delay": 0, "send_log_every": 999,
                             "send_concurrency": 3, "engine": "hybrid",
                             "stop_on_limit": False},
                            report, None)
        return job2, lines2

    job2, lines2 = asyncio.run(go2())
    check("refused peers are skipped on the next run",
          job2.summary.get("total") == 6, job2.summary)
    check("and the skip is announced",
          any("SKIPPING REFUSED" in x.upper() for x in lines2), lines2[:3])


def test_engine_falls_back_loudly_when_direct_is_unavailable():
    print("an unusable direct engine falls back to the bridge, loudly")
    progress_store.clear("a_nodirect")
    d = FakeDriver()

    async def go():
        install_driver(d)
        contacts_store.save("a_nodirect", peers(4))
        R.direct_ctx.refresh_from_driver = lambda drv, acc: _bad()
        R.direct_ctx.has_context = lambda acc: False
        mgr = R.JobManager()
        job = R.Job(job_id="t", kind="send", account="a_nodirect")
        lines = []

        async def report(t):
            lines.append(t)

        await mgr._send_job(job, {"kind": "text", "text": "x"},
                            {"text_send_delay": 0, "send_log_every": 999,
                             "engine": "hybrid"}, report, None)
        return job, lines

    async def _bad():
        return {"ok": False, "code": "no envelope in the dump"}

    job, lines = asyncio.run(go())
    check("it still delivered via the bridge", job.summary.get("sent") == 4, job.summary)
    check("the engine reported is the one actually used",
          job.summary.get("engine") == "bridge", job.summary)
    check("the switch was announced, not silent",
          any("direct_context_missing" in x for x in lines), lines[:2])


def test_stage_card_appears_before_any_send():
    print("the live checklist appears from the first second")
    progress_store.clear("a_stage")
    d = FakeDriver()
    install_driver(d)
    contacts_store.save("a_stage", peers(3))
    live = _FakeLive()

    async def go():
        mgr = R.JobManager()
        job = R.Job(job_id="t", kind="send", account="a_stage")
        await mgr._send_job(job, {"kind": "text", "text": "hi"},
                            {"text_send_delay": 0, "send_log_every": 999,
                             "send_concurrency": 1},
                            lambda _t: asyncio.sleep(0), None, live=live)
        return job

    job = asyncio.run(go())
    first = live.texts[0] if live.texts else ""
    check("a card was painted immediately", "WORKING" in first, first[:60])
    check("it names the browser step", "open browser" in first, first[:120])
    check("it warns about the 2-3 minute start", "minutes" in first, first[:200])
    joined = "\n".join(live.texts)
    check("every step is reported",
          all(s in joined for s in ("check session", "read contacts",
                                    "prepare send path", "deliver messages")),
          joined[:300])
    check("steps get ticked off", "✅" in joined)
    check("it hands over to the send card", "SENDING" in joined)
    check("the job still delivered", job.summary.get("sent") == 3, job.summary)


def test_stage_card_shows_a_failure():
    print("the checklist shows a logged-out account instead of hanging")
    d = FakeDriver(logged_in=False)
    install_driver(d)
    contacts_store.save("a_stage2", peers(3))
    live = _FakeLive()

    async def go():
        mgr = R.JobManager()
        job = R.Job(job_id="t", kind="send", account="a_stage2")
        await mgr._send_job(job, {"kind": "text", "text": "hi"},
                            {"text_send_delay": 0, "send_log_every": 999},
                            lambda _t: asyncio.sleep(0), None, live=live)

    asyncio.run(go())
    joined = "\n".join(live.texts)
    check("the failure is visible on the card", "⚠️" in joined, joined[-200:])
    check("and it says why", "not logged in" in joined.lower())


def test_ui_send_cannot_hang_forever():
    print("a hung UI send is time-boxed, not fatal")
    progress_store.clear("a_hang")

    class Hanger(FakeDriver):
        async def send_text(self, name, text, verify=True):
            self.calls["ui"] += 1
            await asyncio.sleep(30)          # would hang the run
            return R.SendResult(ok=True, to=name, detail="late")

    d = Hanger(fail_peers=["1000"])          # first recipient goes to the UI path
    R._UI_TEXT_TIMEOUT = 0.2                 # keep the test fast
    job, lines, d = asyncio.run(run_send(
        d, account="a_hang", contacts=peers(4), content={"kind": "text", "text": "x"},
        settings={"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 1}))
    check("the run continued past the hang", job.summary.get("sent") == 3, job.summary)
    check("the hung one is counted as failed", job.summary.get("failed") == 1,
          job.summary)
    check("the reason is explicit",
          any("ui_timeout" in x for x in lines), lines[:3])


def test_browserless_decision():
    print("a run only skips the browser when it is truly safe")
    mgr = R.JobManager()
    acc = "a_bl"
    contacts_store.save(acc, [
        {"peer_id": "1", "access_hash": "10", "title": "a"},
        {"peer_id": "2", "access_hash": "20", "title": "b"}])
    R.direct_ctx.has_context = lambda a: True
    R.direct_ctx.newest_capture_age_hours = lambda a: 0.5
    on = {"browserless": True}
    check("OFF by default: a browser-free run must be asked for",
          mgr._can_run_browserless("hybrid", acc, None, {}) is False)
    check("opted in, context fresh, all hashes -> no browser",
          mgr._can_run_browserless("hybrid", acc, None, on) is True)
    check("the bridge engine always opens a browser",
          mgr._can_run_browserless("bridge", acc, None, on) is False)
    check("externally supplied names need the browser",
          mgr._can_run_browserless("hybrid", acc, ["a name"], on) is False)
    R.direct_ctx.newest_capture_age_hours = lambda a: 99.0
    check("a stale context -> browser (only a page can refresh it)",
          mgr._can_run_browserless("hybrid", acc, None, on) is False)
    R.direct_ctx.newest_capture_age_hours = lambda a: 0.5
    R.direct_ctx.has_context = lambda a: False
    check("no session context -> browser",
          mgr._can_run_browserless("hybrid", acc, None, on) is False)
    R.direct_ctx.has_context = lambda a: True
    contacts_store.save("a_bl2", [{"peer_id": "1", "title": "no hash"}])
    check("a contact without an access_hash -> browser (it would need the page)",
          mgr._can_run_browserless("hybrid", "a_bl2", None, on) is False)
    check("no contacts at all -> browser",
          mgr._can_run_browserless("hybrid", "a_bl_empty", None, on) is False)


def test_browserless_run_sends_without_a_page():
    print("a browser-free run delivers with no browser at all")
    progress_store.clear("a_nb")
    opened = {"sessions": 0}
    sent = []

    class Sender:
        account = "a_nb"

        def send_text(self, peer, text):
            sent.append(peer)
            return {"ok": True, "method": "direct/sendMessage"}

        def upload_file(self, path, caption=""):
            return {"ok": True}

        def close(self):
            return None

    class CountingPool(_FakePool):
        def lease(self, account, **kw):
            opened["sessions"] += 1
            return _FakeSessionCtx()

    async def go():
        R.session_pool = CountingPool()
        R.direct_ctx.has_context = lambda a: True
        R.direct_ctx.newest_capture_age_hours = lambda a: 0.2
        contacts_store.save("a_nb", [
            {"peer_id": str(3000 + i), "access_hash": str(7000 + i), "title": f"b{i}"}
            for i in range(5)])
        import direct.sender as ds
        ds.DirectSender = lambda account: Sender()
        mgr = R.JobManager()
        job = R.Job(job_id="t", kind="send", account="a_nb")
        lines = []

        async def report(t):
            lines.append(t)

        await mgr._send_job(job, {"kind": "text", "text": "nb"},
                            {"text_send_delay": 0, "send_log_every": 999,
                             "send_concurrency": 3, "engine": "hybrid",
                             "browserless": True},
                            report, None)
        return job, lines

    job, lines = asyncio.run(go())
    check("no browser session was opened", opened["sessions"] == 0, opened)
    check("everyone got the message", job.summary.get("sent") == 5, job.summary)
    check("the browser-free engine did it", len(sent) == 5, len(sent))
    check("the run reports the browser-free engine",
          job.summary.get("engine") == "hybrid", job.summary)
    check("a preflight card was posted", any("READY TO SEND" in x for x in lines),
          lines[:1])


def test_dry_run_only_touches_the_owner():
    print("the test send goes to Saved Messages only")

    class SelfDriver(FakeDriver):
        async def self_peer_id(self):
            return "5551234"

    d = SelfDriver()
    install_driver(d)
    live = _FakeLive()

    async def go():
        mgr = R.JobManager()
        job = await mgr.run_dry_run("a_dry", {"kind": "text", "text": "hello me"},
                                    {"engine": "bridge"}, lambda t: asyncio.sleep(0),
                                    "98912", live=live)
        await job.task
        return job

    job = asyncio.run(go())
    check("it reported success", job.summary.get("ok") is True, job.summary)
    check("exactly one send happened", d.calls["bridge"] == 1, d.calls)
    check("it measured the real per-message time",
          isinstance(job.summary.get("seconds"), float), job.summary)
    joined = "\n".join(live.texts)
    check("the checklist mentions Saved Messages",
          "Saved Messages" in joined, joined[:200])


def test_dry_run_reports_a_failure_without_sending_a_campaign():
    print("a failing test send stops before any campaign")

    class NoSelf(FakeDriver):
        async def self_peer_id(self):
            return None

    d = NoSelf()
    install_driver(d)
    lines = []

    async def go():
        mgr = R.JobManager()
        job = await mgr.run_dry_run("a_dry2", {"kind": "text", "text": "x"},
                                    {"engine": "bridge"},
                                    lambda t: lines.append(t) or asyncio.sleep(0),
                                    "98912")
        await job.task

    asyncio.run(go())
    check("nothing was sent", d.calls["bridge"] == 0, d.calls)
    check("the problem was reported",
          any("no_self_peer" in str(x) for x in lines), lines[:2])


def test_preflight_card_estimates_from_the_last_run():
    print("the preflight card estimates before anything is sent")
    from bot import cards as C
    fast = C.preflight_card("98912", "hybrid", "Text", 180, 0, 0, 3, 1, 1.9)
    check("it shows the recipient count", "180" in fast, fast)
    check("it shows the pace", "3 at a time" in fast, fast)
    check("it estimates a rate", "msg/s" in fast, fast)
    check("180 at ~1 msg/s is about 3 minutes", "00:02:5" in fast or "00:03:0" in fast,
          fast)
    slow = C.preflight_card("98912", "bridge", "File", 1000, 20, 140, 1, 3, 3.2, 9.5)
    check("skipped and refused are shown",
          "20 already delivered" in slow and "140" in slow, slow)
    check("the file size is shown", "9.5 MB" in slow, slow)
    first = C.preflight_card("98912", "bridge", "Text", 10, 0, 0, 1, 1, None)
    check("a first run says the estimate is assumed",
          "assumes 2s" in first, first)


def main() -> int:
    for fn in (test_sequential_baseline, test_concurrency_overlaps,
               test_concurrency_is_faster, test_lost_upload_is_rebuilt,
               test_dead_upload_stops_instead_of_crawling,
               test_short_flood_wait_is_obeyed, test_long_flood_wait_stops,
               test_peer_flood_can_be_configured_to_continue,
               test_resume_skips_delivered, test_partial_resume,
               test_zero_recipients_uploads_nothing, test_failure_brake,
               test_ui_fallback_when_no_peer_id, test_stop_is_immediate,
               test_not_logged_in, test_force_stop_keeps_the_ledger,
               test_force_stop_TWICE_keeps_the_ledger,
               test_ledger_flush_batch_is_small,
               test_exception_does_not_lose_the_ledger,
               test_batch_results_are_not_discarded_on_limit,
               test_truncated_list_cannot_shrink_the_cache,
               test_ledger_survives_losing_peer_ids,
               test_hybrid_engine_end_to_end,
               test_engine_falls_back_loudly_when_direct_is_unavailable,
               test_stage_card_appears_before_any_send,
               test_stage_card_shows_a_failure,
               test_ui_send_cannot_hang_forever,
               test_file_stops_early_when_the_browser_path_keeps_failing,
               test_upload_failure_stops_instead_of_grinding,
               test_browserless_decision,
               test_browserless_run_sends_without_a_page,
               test_dry_run_only_touches_the_owner,
               test_dry_run_reports_a_failure_without_sending_a_campaign,
               test_preflight_card_estimates_from_the_last_run):
        fn()
    print()
    print(f"{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("ALL SEND-LOOP TESTS PASSED" if code == 0 else "SEND-LOOP TESTS FAILED")
    sys.exit(code)
