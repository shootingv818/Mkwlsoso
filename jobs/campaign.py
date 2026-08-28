"""Campaign engine (the tabchi broadcaster).

Runs a JobState: for each pending recipient it opens the chat and sends the
message, then checkpoints. It is:

- restart-safe: state saved after every recipient; resume continues from pending
- rate-limited: humane random delay between sends + periodic batch cooldown
- dedup: recipients deduped at job creation; already-sent are skipped on resume
- stoppable: a `<job_id>.stop` flag file or Ctrl+C pauses cleanly
- self-braking: auto-pauses after too many consecutive failures

This does NOT bypass Eitaa limits; the delays exist to stay well-behaved.
"""

from __future__ import annotations

import asyncio
import random

from config import config
from capture.browser import open_session
from eitaa.driver import EitaaDriver
from jobs import state as st
from jobs.state import JobState


def create_campaign(account: str, text: str, names: list[str], limit: int | None = None) -> JobState:
    if limit is not None and limit > 0:
        names = names[:limit]
    job = JobState.create(account, text, names)
    job.save()
    return job


async def _apply_send_order(driver, pending: list) -> list:
    """Reorder pending recipients by Eitaa's own last-seen status. OPT-IN.

    Order: online, then seen inside 24h, then "recently", then "last week/month",
    then no signal -- running straight through to the last contact. NOBODY is
    dropped; this only changes who goes first, so the worst case of it being
    wrong is a send in a different order, never a shorter one.

    Deliberately impossible to break a send:
      * OFF (the default) returns the list untouched without importing anything
        from send_order, so the previous behaviour is not merely restored, it is
        never left.
      * ON but anything at all failing -- the setting unreadable, the bridge
        missing, statuses absent, an exception in the tiering -- returns the
        ORIGINAL list and prints why. A broadcast must not be lost over the
        order it goes out in.

    The reason is always printed, never swallowed, so an ordering that quietly
    did nothing can be told apart from one that worked.
    """
    try:
        from bot.store import store as _s
        if not _s.send_order:
            return pending
    except Exception as exc:  # noqa: BLE001
        print(f"[send_order] setting unreadable, keeping the original order: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return pending

    try:
        from eitaa.send_order import order_names, reorder_recipients

        res = await driver.bridge_contacts_list()
        if not isinstance(res, dict):
            # Checked explicitly rather than left to res.get() raising, so the
            # log says what was wrong instead of showing an AttributeError.
            print(f"[send_order] SKIPPED, original order kept: contacts bridge "
                  f"returned {type(res).__name__}, not a dict", flush=True)
            return pending
        if not res.get("ok"):
            print(f"[send_order] SKIPPED, original order kept: contacts bridge "
                  f"said {res.get('code', 'nothing')}", flush=True)
            return pending

        contacts = [c for c in (res.get("contacts") or []) if isinstance(c, dict)]
        if not contacts:
            print("[send_order] SKIPPED, original order kept: the contacts "
                  "bridge returned no usable contact rows", flush=True)
            return pending
        if not any(c.get("status") for c in contacts):
            # An older contacts_list.js is cached in this browser session, so no
            # contact carries a status. Tiering that would put EVERYONE in the
            # bottom tier and look like it had worked.
            print("[send_order] SKIPPED, original order kept: the contacts "
                  "bridge returned no status field (stale cached bridge in this "
                  "browser session)", flush=True)
            return pending

        ordered, stats = reorder_recipients(
            pending, order_names(contacts, now=res.get("server_now")))
        if not stats.get("applied"):
            print(f"[send_order] SKIPPED, original order kept: "
                  f"{stats.get('reason')}", flush=True)
            return pending

        per = stats.get("per_tier") or {}
        print("[send_order] applied: "
              + "  ".join(f"{k}={per.get(k, 0)}" for k in
                          ("online", "today", "recently", "week_or_month",
                           "long_ago"))
              + f"  matched={stats.get('matched')}"
              + f"  no-status={stats.get('unmatched')} (sent last)", flush=True)
        return ordered
    except Exception as exc:  # noqa: BLE001 - ordering is never worth a failed send
        print(f"[send_order] FAILED, original order kept: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return pending


async def _sleep_between(index: int) -> None:
    delay = random.uniform(config.SEND_MIN_DELAY, config.SEND_MAX_DELAY)
    await asyncio.sleep(delay)
    if config.SEND_BATCH_SIZE > 0 and index > 0 and index % config.SEND_BATCH_SIZE == 0:
        await asyncio.sleep(config.SEND_BATCH_COOLDOWN)


async def run_campaign(job: JobState) -> JobState:
    """Run/resume a campaign to completion, pause, or stop."""
    job.status = st.RUNNING
    job.save()

    # Clear any stale stop flag from a previous run.
    if job.stop_flag.exists():
        try:
            job.stop_flag.unlink()
        except OSError:
            pass

    consecutive_failures = 0
    processed = 0

    async with open_session(job.account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            job.status = st.PAUSED
            job.save()
            print("[campaign] not logged in; pausing. run: python cli.py login --account", job.account)
            return job

        pending = [r for r in job.recipients if r.status == st.PENDING]
        pending = await _apply_send_order(driver, pending)
        total_pending = len(pending)
        print(f"[campaign] {job.job_id}: {total_pending} pending of {len(job.recipients)} total")

        for r in pending:
            # Stop requested?
            if job.stop_flag.exists():
                job.status = st.STOPPED
                job.save()
                print("[campaign] stop flag detected; stopping cleanly.")
                try:
                    job.stop_flag.unlink()
                except OSError:
                    pass
                return job

            r.attempts += 1
            try:
                result = await driver.send_text(r.name, job.text, verify=True)
                if result.ok:
                    r.status = st.SENT
                    r.detail = result.detail
                    consecutive_failures = 0
                else:
                    r.status = st.FAILED
                    r.detail = result.detail
                    consecutive_failures += 1
            except Exception as exc:  # noqa: BLE001
                r.status = st.FAILED
                r.detail = f"exception: {exc}"
                consecutive_failures += 1

            import time as _t
            r.updated = _t.time()
            processed += 1
            job.save()  # checkpoint after every recipient

            c = job.counts()
            print(
                f"[campaign] {processed}/{total_pending} -> {r.name!r}: {r.status} "
                f"({r.detail}) | sent={c[st.SENT]} failed={c[st.FAILED]}"
            )

            # Safety brake.
            if consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES:
                job.status = st.PAUSED
                job.save()
                print(
                    f"[campaign] {consecutive_failures} consecutive failures; "
                    "auto-paused. Check the account/selectors, then resume."
                )
                return job

            await _sleep_between(processed)

    if any(r.status == st.PENDING for r in job.recipients):
        job.status = st.PAUSED
    else:
        job.status = st.DONE
    job.save()
    print(f"[campaign] finished with status={job.status}. counts={job.counts()}")
    return job


def request_stop(job_id: str) -> None:
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    flag = config.JOBS_DIR / f"{job_id}.stop"
    flag.write_text("stop", encoding="utf-8")
