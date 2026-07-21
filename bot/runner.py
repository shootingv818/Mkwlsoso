"""Background job runners for the Telegram bot.

Each runner drives the proven EitaaDriver methods, posts English log cards
through an async `report` callback, and is cooperatively stoppable. Jobs are
tracked by JobManager so the panel can show status and stop them.

Restriction detection: the send loop watches for known limit/ban phrases in the
driver's result detail and for a run of consecutive failures; either one pauses
the job and posts a LIMIT card.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from config import config
from capture.browser import open_session
from eitaa.driver import EitaaDriver
from bot import cards

Report = Callable[[str], Awaitable[None]]

# Phrases that indicate the account is limited/blocked by Eitaa.
_LIMIT_PATTERNS = [
    "too many", "flood", "limit", "محدود", "مسدود", "بلاک", "spam",
    "try again later", "بعدا", "بعداً امتحان",
]


def normalize_ir_phone(raw: str) -> str | None:
    """Normalize an Iranian mobile number to +98XXXXXXXXXX. None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        return "+98" + digits
    return None


def expand_range(prefix: str, count: int) -> tuple[list[dict], str | None]:
    """Expand a national-number prefix into up to `count` full contacts.

    Example: prefix "091646" -> 11-digit national numbers 09164600000.. The
    remaining digits are zero-padded sequential. Returns (entries, error).
    """
    p = re.sub(r"\D", "", prefix or "")
    if not p.startswith("0"):
        p = "0" + p
    if not p.startswith("09"):
        return [], "prefix must be an Iranian mobile prefix starting with 09"
    remaining = 11 - len(p)
    if remaining <= 0:
        return [], "prefix already has 11 digits; nothing to fill"
    max_count = 10 ** remaining
    if count > max_count:
        count = max_count
    entries: list[dict] = []
    for i in range(count):
        national = p + str(i).zfill(remaining)
        norm = normalize_ir_phone(national)
        if norm is None:
            continue
        entries.append({"phone": norm, "first": national, "last": ""})
    return entries, None


def _is_limit(detail: str) -> bool:
    low = (detail or "").lower()
    return any(pat in low for pat in _LIMIT_PATTERNS)


@dataclass
class Job:
    job_id: str
    kind: str          # "send" | "contacts"
    account: str
    stop: bool = False
    task: asyncio.Task | None = None
    started: float = field(default_factory=time.time)
    summary: dict = field(default_factory=dict)


class JobManager:
    """Tracks running jobs and enforces one job per account at a time."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._busy: set[str] = set()

    def is_busy(self, account: str) -> bool:
        return account in self._busy

    def active_jobs(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.task and not j.task.done()]

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job:
            job.stop = True
            return True
        return False

    def stop_account(self, account: str) -> int:
        n = 0
        for job in self._jobs.values():
            if job.account == account and job.task and not job.task.done():
                job.stop = True
                n += 1
        return n

    def _new_job(self, kind: str, account: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex[:8], kind=kind, account=account)
        self._jobs[job.job_id] = job
        return job

    async def run_send(self, account: str, content: dict, settings: dict,
                       report: Report, recipients: list[str] | None = None) -> Job:
        job = self._new_job("send", account)
        job.task = asyncio.create_task(
            self._send_job(job, content, settings, report, recipients)
        )
        return job

    async def run_contacts(self, account: str, prefix: str, count: int,
                           settings: dict, report: Report) -> Job:
        job = self._new_job("contacts", account)
        job.task = asyncio.create_task(
            self._contacts_job(job, prefix, count, settings, report)
        )
        return job

    # ---- send job ----
    async def _send_job(self, job: Job, content: dict, settings: dict,
                        report: Report, recipients: list[str] | None) -> None:
        account = job.account
        kind = "File" if content.get("kind") == "file" else "Text"
        delay = float(settings.get("text_send_delay", config.TEXT_SEND_DELAY))
        log_every = int(settings.get("send_log_every", config.SEND_LOG_EVERY))
        self._busy.add(account)
        start = time.time()
        sent = failed = skipped = 0
        try:
            async with open_session(account) as session:
                driver = EitaaDriver(session)
                await driver.open()
                if not await driver.is_logged_in():
                    await report(cards.error_card("send", account, code="not_logged_in",
                                                  detail="account is not logged in"))
                    return

                if recipients is None:
                    contacts = await driver.collect_all_contacts()
                    recipients = [c.get("title", "") for c in contacts if c.get("title")]
                total = len(recipients)
                await report(cards.send_started(account, kind, total, delay))

                consecutive_failures = 0
                for i, name in enumerate(recipients, start=1):
                    if job.stop:
                        break
                    try:
                        if content.get("kind") == "file":
                            res = await driver.send_file(
                                content.get("file_path", ""),
                                caption=content.get("caption", ""),
                                query=name,
                            )
                        else:
                            res = await driver.send_text(name, content.get("text", ""), verify=True)
                        if res.ok:
                            sent += 1
                            consecutive_failures = 0
                        else:
                            failed += 1
                            consecutive_failures += 1
                            if _is_limit(res.detail):
                                await report(cards.restriction_card(account, res.detail, sent))
                                break
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        consecutive_failures += 1
                        await report(cards.error_card(
                            "send_text/send_file", account, target=name,
                            code=type(exc).__name__, detail=str(exc),
                            trace_id=job.job_id,
                        ))

                    if consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES:
                        await report(cards.restriction_card(
                            account, f"{consecutive_failures} consecutive failures", sent))
                        break

                    if i % log_every == 0:
                        await report(cards.send_progress(
                            sent, failed, skipped, total - i, time.time() - start))

                    await asyncio.sleep(delay)

                job.summary = {"sent": sent, "failed": failed, "skipped": skipped, "total": total}
                await report(cards.send_finished(
                    account, kind, sent, failed, skipped, total,
                    time.time() - start, stopped=job.stop))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("send_job", account, code=type(exc).__name__,
                                          detail=str(exc), trace_id=job.job_id))
        finally:
            self._busy.discard(account)

    # ---- contacts job ----
    async def _contacts_job(self, job: Job, prefix: str, count: int,
                            settings: dict, report: Report) -> None:
        account = job.account
        delay = float(settings.get("contact_create_delay", config.CONTACT_CREATE_DELAY))
        log_every = int(settings.get("send_log_every", config.SEND_LOG_EVERY))
        entries, err = expand_range(prefix, count)
        if err:
            await report(cards.error_card("contacts_range", account, code="bad_range", detail=err))
            return
        self._busy.add(account)
        start = time.time()
        added = not_on = invalid = error = 0
        total = len(entries)
        try:
            async with open_session(account) as session:
                driver = EitaaDriver(session)
                await driver.open()
                if not await driver.is_logged_in():
                    await report(cards.error_card("contacts", account, code="not_logged_in",
                                                  detail="account is not logged in"))
                    return
                await report(cards.contacts_started(account, prefix, total, delay))
                await driver.open_contacts_view()

                for i, entry in enumerate(entries, start=1):
                    if job.stop:
                        break
                    try:
                        if i > 1:
                            ok = await driver._reset_to_contacts_view()
                            if not ok:
                                error += 1
                                continue
                        res = await driver._add_one(entry["phone"], entry["first"], entry["last"])
                        status = res.get("status")
                        if status == "added":
                            added += 1
                        elif status == "not_on_eitaa":
                            not_on += 1
                        elif status == "invalid_number":
                            invalid += 1
                        else:
                            error += 1
                    except Exception as exc:  # noqa: BLE001
                        error += 1
                        await report(cards.error_card(
                            "add_contact", account, target=entry.get("phone"),
                            code=type(exc).__name__, detail=str(exc), trace_id=job.job_id))

                    if i % log_every == 0:
                        await report(cards.contacts_progress(
                            added, not_on, invalid, error, total - i))
                    await asyncio.sleep(delay)

                await driver._reset_to_contacts_view()
                job.summary = {"added": added, "not_on": not_on, "invalid": invalid,
                               "error": error, "total": total}
                await report(cards.contacts_finished(
                    account, added, not_on, invalid, error, total,
                    time.time() - start, stopped=job.stop))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("contacts_job", account, code=type(exc).__name__,
                                          detail=str(exc), trace_id=job.job_id))
        finally:
            self._busy.discard(account)


manager = JobManager()
