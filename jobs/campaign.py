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


def create_campaign(account: str, text: str, names: list[str]) -> JobState:
    job = JobState.create(account, text, names)
    job.save()
    return job


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
