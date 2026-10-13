"""Tests for the parallel multi-account send.

Run: python -m bot.tests.test_multi_parallel

No browser and no network: `run_send` is replaced with a fake account job so the
orchestration is what gets exercised. What is pinned here are the things that
were actually wrong or newly risky:

  * Force Stop reached only ONE account. `_multi_children` held a single Job and
    every new account overwrote it, so with two in flight the first survived.
  * a sliding window, not fixed pairs: the moment one account finishes the next
    starts, so a small account paired with a huge one leaves no idle slot.
  * width 1 must behave exactly like the original sequential run.
  * the same account ticked twice must not fight itself for the session lock.
  * two accounts at the configured delay would double the rate leaving this
    server's single IP, so the per-account delay is scaled by the width.
  * the live card must name every account in flight and never imply an execution
    order that a parallel run does not have.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_multi_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")
os.environ["MKWL_MULTI_STAGGER"] = "0"        # deterministic and fast


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

from config import config  # noqa: E402
from bot import cards, contacts_store  # noqa: E402
import bot.runner as R  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


CONTENT = {"kind": "text", "text": "hello"}
BASE_SETTINGS = {"text_send_delay": 3, "send_log_every": 1000,
                 "send_concurrency": 1}


def seed(accounts: dict[str, int]) -> list[tuple[str, str]]:
    """Give each account a cached contact count so phase 1 does no work."""
    for acc, n in accounts.items():
        contacts_store.save(acc, [{"peer_id": str(i), "access_hash": "h",
                                   "title": f"c{i}"} for i in range(n)])
    return [(a, a) for a in accounts]


class Recorder:
    """Replaces JobManager.run_send with a fake per-account job."""

    def __init__(self, mgr: R.JobManager, durations: dict[str, float],
                 *, hang: set[str] | None = None) -> None:
        self.mgr = mgr
        self.durations = durations
        self.hang = hang or set()
        self.started: list[str] = []
        self.finished: list[str] = []
        self.concurrent = 0
        self.peak = 0
        self.settings_seen: list[dict] = []
        self.timeline: list[tuple[str, str]] = []   # (event, account)

    def install(self) -> None:
        async def fake_run_send(account, content, settings, report, live=None,
                                account_phone=None, agg=None, recipients=None):
            self.settings_seen.append(dict(settings or {}))
            job = self.mgr._new_job("send", account)

            async def body():
                self.started.append(account)
                self.timeline.append(("start", account))
                self.concurrent += 1
                self.peak = max(self.peak, self.concurrent)
                try:
                    if account in self.hang:
                        # Never finishes on its own: only a stop can end it.
                        while not job.stop:
                            await asyncio.sleep(0.005)
                    else:
                        end = asyncio.get_event_loop().time() + \
                            self.durations.get(account, 0.02)
                        while asyncio.get_event_loop().time() < end:
                            if job.stop:
                                break
                            await asyncio.sleep(0.005)
                    total = contacts_store.count(account)
                    if agg is not None:
                        await agg.update(account, sent=total, total=total,
                                         state="stopped" if job.stop else "done")
                    job.summary = {"sent": 0 if job.stop else total,
                                   "failed": 0}
                finally:
                    self.concurrent -= 1
                    self.finished.append(account)
                    self.timeline.append(("end", account))

            job.task = asyncio.create_task(body())
            return job

        self.mgr.run_send = fake_run_send  # type: ignore[assignment]


class FakeLive:
    def __init__(self):
        self.texts: list[str] = []

    async def set(self, text, force=False):
        self.texts.append(text)

    async def flush(self):
        return None


async def drive(accounts: list[tuple[str, str]], width: int,
                durations: dict[str, float], *, hang: set[str] | None = None,
                stop_after: float | None = None, force: bool = False):
    mgr = R.JobManager()
    rec = Recorder(mgr, durations, hang=hang)
    rec.install()
    live = FakeLive()
    reports: list[str] = []

    async def report(text):
        reports.append(text)

    settings = dict(BASE_SETTINGS, multi_parallel=width)
    multi = await mgr.run_send_multi(accounts, CONTENT, settings, report,
                                     live=live)
    if stop_after is not None:
        await asyncio.sleep(stop_after)
        mgr.stop(multi.job_id, force=force)
    await multi.task
    return mgr, rec, multi, live, reports


# --------------------------------------------------------------------------

def test_width_one_is_sequential() -> None:
    print("\n1) width 1 behaves exactly like the old sequential run")
    accounts = seed({"a1": 5, "a2": 5, "a3": 5})
    _, rec, multi, _, _ = run(drive(accounts, 1, {a: 0.03 for a, _ in accounts}))
    check("every account ran", sorted(rec.finished) == ["a1", "a2", "a3"],
          str(rec.finished))
    check("never more than one at a time", rec.peak == 1, f"peak {rec.peak}")
    check("order is the tick order", rec.started == ["a1", "a2", "a3"],
          str(rec.started))
    check("the delay is untouched",
          all(s.get("text_send_delay") == 3 for s in rec.settings_seen),
          str([s.get("text_send_delay") for s in rec.settings_seen]))


def test_width_two_runs_two() -> None:
    print("\n2) width 2 really runs two at once")
    accounts = seed({"b1": 5, "b2": 5, "b3": 5, "b4": 5})
    _, rec, _, _, _ = run(drive(accounts, 2, {a: 0.05 for a, _ in accounts}))
    check("every account ran", len(rec.finished) == 4, str(rec.finished))
    check("two were in flight together", rec.peak == 2, f"peak {rec.peak}")
    check("never three", rec.peak <= 2, f"peak {rec.peak}")


def test_sliding_window_not_pairs() -> None:
    print("\n3) a sliding window, not fixed pairs")
    # c1 is quick, c2 is slow. With fixed pairs c3 would wait for c2; with a
    # sliding window it starts the moment c1 ends -- while c2 is still going.
    accounts = seed({"c1": 5, "c2": 5, "c3": 5})
    _, rec, _, _, _ = run(drive(accounts, 2,
                                {"c1": 0.02, "c2": 0.30, "c3": 0.02}))
    tl = rec.timeline
    end_c1 = tl.index(("end", "c1"))
    start_c3 = tl.index(("start", "c3"))
    end_c2 = tl.index(("end", "c2"))
    check("c3 starts right after c1 ends", start_c3 == end_c1 + 1,
          f"end c1 at {end_c1}, start c3 at {start_c3}")
    check("c3 starts BEFORE the slow c2 finishes", start_c3 < end_c2,
          f"start c3 {start_c3} vs end c2 {end_c2}")


def test_force_stop_reaches_every_account() -> None:
    print("\n4) Force Stop kills BOTH accounts, not just the last one")
    # The bug: _multi_children held ONE Job, so the first account survived.
    accounts = seed({"d1": 5, "d2": 5})
    mgr, rec, multi, _, _ = run(drive(
        accounts, 2, {}, hang={"d1", "d2"}, stop_after=0.08, force=True))
    check("both accounts had started", sorted(rec.started) == ["d1", "d2"],
          str(rec.started))
    check("both accounts ended", sorted(rec.finished) == ["d1", "d2"],
          str(rec.finished))
    check("nothing is still in flight", rec.concurrent == 0,
          str(rec.concurrent))
    check("the child list was cleaned up",
          not mgr._multi_children.get(multi.job_id), str(mgr._multi_children))


def test_graceful_stop_reaches_every_account() -> None:
    print("\n5) a graceful Stop also reaches both")
    accounts = seed({"e1": 5, "e2": 5})
    _, rec, _, _, _ = run(drive(accounts, 2, {}, hang={"e1", "e2"},
                                stop_after=0.08, force=False))
    check("both stopped without being cancelled",
          sorted(rec.finished) == ["e1", "e2"], str(rec.finished))


def test_duplicate_accounts_deduped() -> None:
    print("\n6) the same account ticked twice must not fight itself")
    seed({"f1": 5})
    accounts = [("f1", "f1"), ("f1", "f1"), ("f1", "f1")]
    _, rec, _, _, _ = run(drive(accounts, 2, {"f1": 0.02}))
    check("it ran once, not three times", rec.started == ["f1"],
          str(rec.started))


def test_shared_budget_scales_the_delay() -> None:
    print("\n7) two accounts must not double the rate leaving this IP")
    accounts = seed({"g1": 5, "g2": 5})
    _, rec, _, _, _ = run(drive(accounts, 2, {a: 0.02 for a, _ in accounts}))
    delays = [s.get("text_send_delay") for s in rec.settings_seen]
    if config.MULTI_SHARE_BUDGET:
        check("each account's delay is scaled by the width",
              all(d == 6 for d in delays), str(delays))
    else:
        check("budget sharing is off, so the delay is untouched",
              all(d == 3 for d in delays), str(delays))


def test_one_account_failing_does_not_stop_others() -> None:
    print("\n8) a crashing account must not take the run down")
    accounts = seed({"h1": 5, "h2": 5, "h3": 5})
    reports: list[str] = []
    holder: dict = {}

    # Everything has to share ONE event loop: starting the run on one loop and
    # awaiting its task on another is what "future belongs to a different loop"
    # means.
    async def scenario():
        mgr = R.JobManager()
        rec = Recorder(mgr, {a: 0.02 for a, _ in accounts})
        rec.install()
        inner = mgr.run_send

        async def sometimes_explode(account, *a, **k):
            if account == "h2":
                raise RuntimeError("browser died")
            return await inner(account, *a, **k)

        mgr.run_send = sometimes_explode  # type: ignore[assignment]

        async def report(text):
            reports.append(text)

        multi = await mgr.run_send_multi(
            accounts, CONTENT, dict(BASE_SETTINGS, multi_parallel=2),
            report, live=FakeLive())
        await multi.task
        holder["rec"] = rec

    run(scenario())
    rec = holder["rec"]
    check("the other two still ran", sorted(rec.finished) == ["h1", "h3"],
          str(rec.finished))
    check("an error card was posted",
          any("ERROR" in t.upper() for t in reports), str(len(reports)))
    check("the run still posted its summary",
          any("MULTI" in t.upper() for t in reports))


def test_card_shows_every_running_account() -> None:
    print("\n9) the live card fits a parallel run")
    accs = [
        {"phone": "9891", "sent": 449, "failed": 0, "total": 449, "state": "done"},
        {"phone": "9892", "sent": 210, "failed": 3, "total": 500, "state": "running"},
        {"phone": "9893", "sent": 74, "failed": 0, "total": 300, "state": "running"},
        {"phone": "9894", "sent": 0, "failed": 0, "total": 800, "state": "pending"},
    ]
    text = cards.live_send_multi(accs, "9892", 733, 3, 2049, 300.0,
                                 kind="Text", parallel=2)
    check("it names BOTH running accounts",
          "9892" in text.split("Now")[1].split("\n")[0]
          and "9893" in text.split("Now")[1].split("\n")[0],
          text.split("Now")[1].split("\n")[0])
    check("the mode is stated", "parallel" in text)
    check("every account gets its own bar",
          text.count("\u25b0") + text.count("\u25b1") >= 40)
    check("the waiting one is summarised", "1 waiting" in text)
    check("no misleading 1. 2. 3. ordering",
          "\n1. " not in text and "\n2. " not in text)

    seq = cards.live_send_multi(accs, "9892", 733, 3, 2049, 300.0,
                                kind="Text", parallel=1)
    check("width 1 says one at a time", "one account at a time" in seq)


def test_card_stays_within_telegram_limit() -> None:
    print("\n10) a 50-account run must not blow the message limit")
    accs = [{"phone": f"98900{i:05d}", "sent": i, "failed": 0,
             "total": 100, "state": "done"} for i in range(48)]
    accs += [{"phone": "98999999998", "sent": 5, "failed": 0, "total": 100,
              "state": "running"},
             {"phone": "98999999999", "sent": 0, "failed": 0, "total": 100,
              "state": "pending"}]
    text = cards.live_send_multi(accs, "98999999998", 1200, 0, 5000, 900.0,
                                 kind="Text", parallel=2)
    check("the card is well under 4096 chars", len(text) < 3500, str(len(text)))
    check("the running account is still shown", "98999999998" in text)
    check("the hidden ones are counted", "more finished" in text)


def main() -> int:
    print("=" * 68)
    print("MULTI-ACCOUNT PARALLEL SEND")
    print("=" * 68)
    try:
        test_width_one_is_sequential()
        test_width_two_runs_two()
        test_sliding_window_not_pairs()
        test_force_stop_reaches_every_account()
        test_graceful_stop_reaches_every_account()
        test_duplicate_accounts_deduped()
        test_shared_budget_scales_the_delay()
        test_one_account_failing_does_not_stop_others()
        test_card_shows_every_running_account()
        test_card_stays_within_telegram_limit()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL MULTI PARALLEL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
