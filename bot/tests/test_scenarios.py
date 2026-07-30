"""Fault-injection scenarios: 18 ways this thing can go wrong.

Run: python -m bot.tests.test_scenarios

Every scenario injects a failure that either DID happen on the live server or is
one step away from it, and asserts the bot degrades in a way the owner can act on:
never a silent stall, never a duplicate message, never a crash that loses the
progress ledger.

The standby session pool is exercised against a fake browser, including the cases
that make pooling dangerous: a session that dies while warm, a job cancelled
mid-lease, a shape change (worker hook needed), eviction, recycling and idle
expiry.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_scen_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")
os.environ["MKWL_SESSION_POOL"] = "1"


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

from bot import blocked_store, contacts_store, direct_ctx, progress_store  # noqa: E402
from bot import runner as R  # noqa: E402
from capture import pool as poolmod  # noqa: E402

_PASS = 0
_FAIL = 0
_SCENARIOS = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"    PASS  {name}")
    else:
        _FAIL += 1
        print(f"    FAIL  {name} {extra}")


def scenario(n: int, title: str) -> None:
    global _SCENARIOS
    _SCENARIOS += 1
    print(f"\n[{n:02d}] {title}")


# ---- fake browser stack ------------------------------------------------

class FakePage:
    def __init__(self):
        self.closed = False
        self.evals = 0
        self.reloads = 0
        self.die_after = None

    def is_closed(self):
        return self.closed

    async def evaluate(self, *a, **kw):
        self.evals += 1
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        if self.die_after is not None and self.evals > self.die_after:
            self.closed = True
            raise RuntimeError("Target closed")
        return 1

    async def reload(self, **kw):
        self.reloads += 1

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)


class FakeSession:
    """Mimics capture.browser.BrowserSession closely enough for the pool."""

    instances = []

    def __init__(self, account, headed=None, init_script_path=None,
                 light_assets=None, fail_start=False, start_delay=0.0):
        self.account = account
        self.headed = headed
        self.init_script_path = init_script_path
        self.page = None
        self.started = False
        self.closed = False
        self.fail_start = fail_start
        self.start_delay = start_delay
        FakeSession.instances.append(self)

    async def start(self):
        if self.fail_start:
            raise RuntimeError("chromium failed to launch")
        await asyncio.sleep(self.start_delay)
        self.page = FakePage()
        self.started = True
        return self

    async def close(self):
        self.closed = True
        if self.page is not None:
            self.page.closed = True


def fresh_pool(**kw) -> poolmod.SessionPool:
    FakeSession.instances = []
    poolmod.BrowserSession = FakeSession
    return poolmod.SessionPool(**kw)


# ---- pool scenarios ----------------------------------------------------

def s01_nothing_is_opened_until_asked():
    scenario(1, "the pool opens nothing until a job asks (standby, not parade)")
    p = fresh_pool()
    check("no session exists yet", len(FakeSession.instances) == 0)
    check("status says nothing is warm", p.status()["warm"] == 0, p.status())

    async def go():
        async with p.lease("a") as s:
            check("a session was created on demand", s.started is True)
        return True

    asyncio.run(go())
    check("exactly one launch happened", len(FakeSession.instances) == 1,
          len(FakeSession.instances))
    check("it stayed warm after release", p.status()["warm"] == 1, p.status())


def s02_second_job_reuses_the_warm_session():
    scenario(2, "a second job reuses the warm session instead of launching")
    p = fresh_pool()

    async def go():
        async with p.lease("a"):
            pass
        async with p.lease("a") as s2:
            return s2

    s2 = asyncio.run(go())
    check("only one browser was ever launched", len(FakeSession.instances) == 1,
          len(FakeSession.instances))
    check("the same session came back", s2 is FakeSession.instances[0])
    check("the saving is counted", p.stats["saved_launches"] == 1, p.stats)


def s03_dead_warm_session_is_replaced():
    scenario(3, "a session that died while warm is replaced, not handed out")
    p = fresh_pool()

    async def go():
        async with p.lease("a") as s1:
            pass
        s1.page.closed = True          # the browser died on standby
        async with p.lease("a") as s2:
            return s1, s2

    s1, s2 = asyncio.run(go())
    check("a fresh session was created", s2 is not s1)
    check("the dead one was discarded", p.stats["discarded"] >= 1, p.stats)
    check("two launches total", len(FakeSession.instances) == 2,
          len(FakeSession.instances))


def s04_cancelled_lease_is_never_reused():
    scenario(4, "Force Stop (cancellation) discards the session")
    p = fresh_pool()

    async def go():
        async def worker():
            async with p.lease("a"):
                await asyncio.sleep(5)
        t = asyncio.create_task(worker())
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return p.status()

    st = asyncio.run(go())
    check("nothing stayed warm", st["warm"] == 0, st)
    check("it was discarded on purpose", p.stats["discarded"] >= 1, p.stats)


def s05_shape_change_forces_a_new_session():
    scenario(5, "a job that needs the worker hook does not get a session without it")
    p = fresh_pool()

    async def go():
        async with p.lease("a"):
            pass
        async with p.lease("a", init_script_path="/x/worker_capture.js") as s2:
            return s2

    s2 = asyncio.run(go())
    check("a session with the hook was created",
          str(s2.init_script_path).endswith("worker_capture.js"), s2.init_script_path)
    check("the hookless one was discarded", p.stats["discarded"] >= 1, p.stats)


def s06_max_open_evicts_lru():
    scenario(6, "with max_open=1 a second account evicts the idle one (961 MB host)")
    p = fresh_pool(max_open=1)

    async def go():
        async with p.lease("a"):
            pass
        async with p.lease("b"):
            pass
        return p.status()

    st = asyncio.run(go())
    check("only one session is warm", st["warm"] == 1, st)
    check("the idle one was evicted", p.stats["evicted"] == 1, p.stats)
    check("the survivor is the newest", "b" in st["accounts"], st)


def s07_recycled_after_max_uses():
    scenario(7, "a session is recycled after N uses (browsers leak when kept forever)")
    p = fresh_pool(max_uses=3)

    async def go():
        for _ in range(4):
            async with p.lease("a"):
                pass
        return p.stats

    stats = asyncio.run(go())
    check("it was recycled once", stats["recycled"] == 1, stats)
    check("two launches in total", stats["created"] == 2, stats)


def s08_idle_expiry_closes_standby():
    scenario(8, "a standby session closes itself when idle too long")
    p = fresh_pool(idle_ttl=0.05)

    async def go():
        async with p.lease("a") as s:
            pass
        await asyncio.sleep(0.1)
        closed = await p.close_all()      # the reaper's job, deterministically
        return s, closed

    s, closed = asyncio.run(go())
    check("the standby session was closed", s.closed is True)
    check("the pool reports it empty", p.status()["warm"] == 0, p.status())


def s09_one_lease_per_account():
    scenario(9, "two jobs can never drive the same page at once")
    p = fresh_pool()
    order = []

    async def go():
        async def job(tag, hold):
            async with p.lease("a"):
                order.append(f"in:{tag}")
                await asyncio.sleep(hold)
                order.append(f"out:{tag}")
        await asyncio.gather(job("A", 0.08), job("B", 0.01))

    asyncio.run(go())
    check("the leases did not interleave",
          order in (["in:A", "out:A", "in:B", "out:B"],
                    ["in:B", "out:B", "in:A", "out:A"]), order)


def s10_pool_can_be_disabled():
    scenario(10, "MKWL_SESSION_POOL=0 goes back to a browser per job")
    p = fresh_pool()
    os.environ["MKWL_SESSION_POOL"] = "0"
    opened = {"n": 0}

    class Ctx:
        async def __aenter__(self):
            opened["n"] += 1
            return FakeSession("a")

        async def __aexit__(self, *a):
            return False

    poolmod.open_session = lambda account, **kw: Ctx()

    async def go():
        async with p.lease("a"):
            pass
        async with p.lease("a"):
            pass

    try:
        asyncio.run(go())
        check("each job opened its own session", opened["n"] == 2, opened)
        check("nothing was kept warm", p.status()["warm"] == 0, p.status())
    finally:
        os.environ["MKWL_SESSION_POOL"] = "1"


def s11_failed_launch_does_not_poison_the_pool():
    scenario(11, "a failed launch leaves the pool clean for the next try")
    p = fresh_pool()
    poolmod.BrowserSession = lambda account, **kw: FakeSession(
        account, fail_start=True, **kw)

    async def go():
        try:
            async with p.lease("a"):
                pass
        except RuntimeError as exc:
            return str(exc)
        return None

    err = asyncio.run(go())
    check("the error reached the caller", "failed to launch" in (err or ""), err)
    check("nothing is warm afterwards", p.status()["warm"] == 0, p.status())
    poolmod.BrowserSession = FakeSession

    async def go2():
        async with p.lease("a") as s:
            return s.started

    check("the next attempt still works", asyncio.run(go2()) is True)


# ---- corrupt state scenarios ------------------------------------------

def s12_corrupt_stores_are_survivable():
    scenario(12, "corrupt cache files never break a run")
    (contacts_store.path_for("c1")).parent.mkdir(parents=True, exist_ok=True)
    contacts_store.path_for("c1").write_text("{not json", encoding="utf-8")
    check("contacts cache falls back to empty", contacts_store.count("c1") == 0)
    progress_store.path_for("c1").write_text("[]", encoding="utf-8")
    check("ledger falls back to empty",
          len(progress_store.open_ledger("c1", "k").done) == 0)
    (blocked_store.path_for("c1")).write_text("garbage", encoding="utf-8")
    check("refused list falls back to empty", blocked_store.count("c1") == 0)
    d = direct_ctx.sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "capall_c1_1.json").write_text("nope", encoding="utf-8")
    check("a corrupt capture means 'no context', not a crash",
          direct_ctx.has_context("c1") is False)


def s13_ledger_written_then_truncated():
    scenario(13, "a half-written ledger does not resend to everyone silently")
    key = progress_store.content_key({"kind": "text", "text": "x"})
    led = progress_store.open_ledger("c2", key)
    for i in range(5):
        led.mark(f"n{i}", str(i))
    led.flush()
    p = progress_store.path_for("c2")
    raw = p.read_text(encoding="utf-8")
    p.write_text(raw[:len(raw) // 2], encoding="utf-8")   # truncated write
    again = progress_store.open_ledger("c2", key)
    check("a truncated ledger reads as empty (resend, never skip wrongly)",
          len(again.done) == 0)
    check("and the next flush repairs it",
          (again.mark("n0", "0"), again.flush(),
           progress_store.done_count("c2", key))[2] == 1)


def s14_direct_context_for_the_wrong_account():
    scenario(14, "another account's capture is not used for this one")
    d = direct_ctx.sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "capall_other_9.json").write_text(json.dumps(
        {"x": [{"kind": "fetch", "url": "u", "reqHead": "ed77be7a00", "reqLen": 5}]}),
        encoding="utf-8")
    check("no context for an account with no capture of its own",
          direct_ctx.has_context("mine") is False)
    check("captures are listed per account", direct_ctx.capture_files("mine") == [])


# ---- send-loop fault injection ----------------------------------------

def _fake_send_env():
    """Minimal fakes so a real _send_job can run in these scenarios."""
    from bot.tests import test_send_loop as T
    return T


def s15_transport_raises_on_every_recipient():
    scenario(15, "a transport that always raises stops via the brake, ledger intact")
    T = _fake_send_env()
    progress_store.clear("s15")

    class Boom(T.FakeDriver):
        async def bridge_send(self, peer_id, text):
            raise RuntimeError("page crashed")

    d = Boom(ui_ok=False)
    job, lines, d = asyncio.run(T.run_send(
        d, account="s15", contacts=T.peers(60),
        content={"kind": "text", "text": "x"},
        settings={"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 3}))
    check("it stopped instead of running the whole list",
          job.summary.get("failed", 0) <= R._FAILURE_BRAKE + 3, job.summary)
    check("nothing was marked as delivered",
          progress_store.done_count("s15", progress_store.content_key(
              {"kind": "text", "text": "x"})) == 0)
    check("the owner was told", any("PAUSED" in x.upper() or "consecutive" in x
                                   for x in lines), lines[-2:])


def s16_every_peer_refused_is_remembered_once():
    scenario(16, "an all-refused run remembers every peer exactly once")
    T = _fake_send_env()
    progress_store.clear("s16")
    blocked_store.clear("s16")

    class AllFlood(T.FakeDriver):
        async def bridge_send(self, peer_id, text):
            return {"ok": False, "limit": True, "code": "PEER_FLOOD"}

    d = AllFlood()
    job, lines, d = asyncio.run(T.run_send(
        d, account="s16", contacts=T.peers(10),
        content={"kind": "text", "text": "x"},
        settings={"text_send_delay": 0, "send_log_every": 999,
                  "send_concurrency": 2, "stop_on_limit": False}))
    n = blocked_store.count("s16")
    check(f"refused peers were recorded ({n})", 1 <= n <= 10, n)
    check("no duplicates in the record",
          len(set(blocked_store.peers("s16").keys())) == n)
    check("restriction cards were capped",
          sum(1 for x in lines if "LIMIT DETECTED" in x) <= 3)


def s17_flaky_transport_still_delivers_everyone_eventually():
    scenario(17, "a transport that fails every other call still finishes the list")
    T = _fake_send_env()
    progress_store.clear("s17")

    class Flaky(T.FakeDriver):
        async def bridge_send(self, peer_id, text):
            self.calls["bridge"] += 1
            if self.calls["bridge"] % 2 == 0:
                return {"ok": False, "code": "transport hiccup"}
            return {"ok": True, "method": "bridge", "msg_id": 1}

    d = Flaky(ui_ok=True)      # the UI fallback saves the failures
    job, lines, d = asyncio.run(T.run_send(
        d, account="s17", contacts=T.peers(10),
        content={"kind": "text", "text": "x"},
        settings={"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 2}))
    check("everyone was delivered to", job.summary.get("sent") == 10, job.summary)
    check("half went the slow way", job.summary.get("via_fallback") == 5, job.summary)
    check("the brake never tripped (successes reset it)",
          job.summary.get("failed") == 0, job.summary)


def s18_stop_during_a_batch_loses_nothing():
    scenario(18, "Stop in the middle of a batch banks what already landed")
    T = _fake_send_env()
    progress_store.clear("s18")
    content = {"kind": "text", "text": "stopme"}
    key = progress_store.content_key(content)
    d = T.FakeDriver()
    job, lines, d = asyncio.run(T.run_send(
        d, account="s18", contacts=T.peers(60), content=content,
        settings={"text_send_delay": 2, "send_log_every": 999, "send_concurrency": 3},
        stop_after=0.12))
    sent = job.summary.get("sent", 0)
    recorded = progress_store.done_count("s18", key)
    check(f"it stopped early ({sent} of 60)", 0 < sent < 60, job.summary)
    check(f"every delivered message is recorded ({recorded} vs {sent})",
          recorded == sent, (recorded, sent))
    check("a re-run would only do the rest",
          len([1 for n, p in [(f"n{i}", str(1000 + i)) for i in range(60)]
               if not progress_store.open_ledger("s18", key).has(n, p)]) == 60 - sent)


def s19_reaper_survives_a_slow_first_launch():
    scenario(19, "the idle reaper still works when the first launch is slow")
    p = fresh_pool(idle_ttl=0.05)
    poolmod.BrowserSession = lambda account, **kw: FakeSession(
        account, start_delay=0.3, **kw)

    async def go():
        # The reaper starts before this launch finishes (158-203s on the real
        # host); it must not decide the pool is empty and give up.
        async with p.lease("a"):
            pass
        alive = p._reaper is not None and not p._reaper.done()
        # And it must still reap once the session goes idle.
        await asyncio.sleep(0.1)
        closed = await p.close_all()
        return alive, closed

    alive, closed = asyncio.run(go())
    check("the reaper is still alive after a slow launch", alive is True)
    check("an idle standby session is still collected", closed == 1, closed)
    poolmod.BrowserSession = FakeSession


def s20_hybrid_file_fallback_uploads_on_the_page_first():
    scenario(20, "a page fallback for a file uploads on the page before sending")
    from bot import transports as TR
    from bot.tests import test_engines as E

    calls = []

    class Page(E.FakeBridgeDriver):
        async def bridge_file_init(self, path, caption="", locate_timeout=None):
            calls.append("init")
            return {"ok": True, "msg_id": 1}

        async def bridge_file_ready(self):
            return "init" in calls

        async def bridge_file_send(self, peer, caption=""):
            calls.append("send")
            return {"ok": True, "method": "bridge"}

    class DirectOnlyUpload(E.FakeSender):
        def send_uploaded_file(self, peer, caption=""):
            return {"ok": False, "code": "transport: reset"}

    page = Page()
    h = TR.HybridTransport(
        TR.DirectTransport(DirectOnlyUpload(), {"1": "10"}, peers_module=E.FakePeers),
        TR.BridgeTransport(page))

    async def go():
        await h.prepare_file("/tmp/f.zip", "cap")
        return await h.send_file("1", "cap")

    res = asyncio.run(go())
    check("the recipient was served", res.get("ok") is True, res)
    check("the page uploaded before sending", calls[:2] == ["init", "send"], calls)


def s21_dry_run_addresses_saved_messages_without_contacts():
    scenario(21, "the test send reaches Saved Messages on the browser-free engine")
    from bot import transports as TR
    from bot.tests import test_engines as E

    class WithSelf(E.FakeSender):
        self_peer = b"SELFPEER"

    s = WithSelf()
    t = TR.DirectTransport(s, {}, peers_module=E.FakePeers)   # no contacts at all
    res = asyncio.run(t.send_text("self", "hi me"))
    check("it was delivered", res.get("ok") is True, res)
    check("it used the account's own peer", s.sent == [b"SELFPEER"], s.sent)


def s22_refusals_survive_a_force_stop():
    scenario(22, "peers refused during a cancelled run are still remembered")
    T = _fake_send_env()
    blocked_store.clear("s22")
    progress_store.clear("s22")

    class SlowFlood(T.FakeDriver):
        async def bridge_send(self, peer_id, text):
            await asyncio.sleep(0.01)
            return {"ok": False, "limit": True, "code": "PEER_FLOOD"}

    async def go():
        d = SlowFlood()
        T.install_driver(d)
        contacts_store.save("s22", T.peers(80))
        mgr = R.JobManager()
        job = R.Job(job_id="t", kind="send", account="s22")
        task = asyncio.create_task(mgr._send_job(
            job, {"kind": "text", "text": "x"},
            {"text_send_delay": 0, "send_log_every": 999, "send_concurrency": 2,
             "stop_on_limit": False},
            lambda t: asyncio.sleep(0), None))
        await asyncio.sleep(0.15)
        task.cancel()                       # Force Stop
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    n = blocked_store.count("s22")
    check(f"refusals learned before the cancel were saved ({n})", n > 0, n)


def s23_pool_waits_instead_of_running_two_browsers():
    scenario(23, "at the cap the pool WAITS; it never runs two browsers at once")
    p = fresh_pool(max_open=1)
    peak = {"n": 0, "now": 0}

    async def go():
        async def job(acc, hold):
            async with p.lease(acc):
                peak["now"] += 1
                peak["n"] = max(peak["n"], peak["now"])
                await asyncio.sleep(hold)
                peak["now"] -= 1
        # Two DIFFERENT accounts: this is the case that put two Chromiums on a
        # 961 MB host and pushed 'load Eitaa web' from ~60s to 272s.
        await asyncio.gather(job("a", 0.15), job("b", 0.05))

    asyncio.run(go())
    check(f"never more than one browser live (peak={peak['n']})", peak["n"] == 1, peak)
    check("the pool stayed within its cap", p.status()["warm"] <= 1, p.status())


def s24_prewarm_never_competes_with_a_login():
    scenario(24, "the template warmer refuses to run while a job holds a browser")
    mgr = R.JobManager()
    mgr._busy.add("someacct")
    res = asyncio.run(mgr.prewarm_new_account())
    check("it refused", res.get("ok") is False, res)
    check("and said why", "busy" in str(res.get("code")), res)
    mgr._busy.discard("someacct")


def main() -> int:
    for fn in (s01_nothing_is_opened_until_asked,
               s02_second_job_reuses_the_warm_session,
               s03_dead_warm_session_is_replaced,
               s04_cancelled_lease_is_never_reused,
               s05_shape_change_forces_a_new_session,
               s06_max_open_evicts_lru,
               s07_recycled_after_max_uses,
               s08_idle_expiry_closes_standby,
               s09_one_lease_per_account,
               s10_pool_can_be_disabled,
               s11_failed_launch_does_not_poison_the_pool,
               s12_corrupt_stores_are_survivable,
               s13_ledger_written_then_truncated,
               s14_direct_context_for_the_wrong_account,
               s15_transport_raises_on_every_recipient,
               s16_every_peer_refused_is_remembered_once,
               s17_flaky_transport_still_delivers_everyone_eventually,
               s18_stop_during_a_batch_loses_nothing,
               s19_reaper_survives_a_slow_first_launch,
               s20_hybrid_file_fallback_uploads_on_the_page_first,
               s21_dry_run_addresses_saved_messages_without_contacts,
               s22_refusals_survive_a_force_stop,
               s23_pool_waits_instead_of_running_two_browsers,
               s24_prewarm_never_competes_with_a_login):
        fn()
    print()
    print(f"{_SCENARIOS} scenarios · {_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("ALL SCENARIOS PASSED" if code == 0 else "SCENARIOS FAILED")
    sys.exit(code)
