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
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from config import config
from capture.browser import open_session
from capture.pool import pool as session_pool
from eitaa.driver import EitaaDriver, SendResult
from bot import blocked_store          # noqa: F401 - used in the send loop
from bot import cards
from bot import contacts_store
from bot import direct_ctx
from bot import progress_store
from bot import transports

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


# How many consecutive failures mean "this session is broken, stop". A real
# server-side restriction is detected by _is_limit and stops immediately, so this
# only guards against a dead session. The old value (5) killed a 1,099-contact
# run at recipient 300 because one page hiccup produced a short failure streak.
_FAILURE_BRAKE = max(15, config.MAX_CONSECUTIVE_FAILURES)

# How many times a lost in-page upload may be rebuilt during one send.
_MAX_FILE_REINITS = 3

# How many recipients may be served by the per-recipient UI upload before the run
# gives up. Each one measured ~25 SECONDS live, so letting a 1,000-contact list
# crawl down this path means an 7-hour run that usually fails anyway. Stopping
# with a clear reason is better: the ledger makes the re-run resume for free.
_MAX_UI_FILE_FALLBACKS = 5

# Hard time budget for one send through the slow browser-UI path. Without this a
# single stuck recipient froze a whole run: measured live, a UI file send on the
# first recipient produced 13 minutes of total silence and zero deliveries.
_UI_FILE_TIMEOUT = float(os.environ.get("MKWL_UI_FILE_TIMEOUT", "150"))
_UI_TEXT_TIMEOUT = float(os.environ.get("MKWL_UI_TEXT_TIMEOUT", "60"))

# How long to wait for the app to switch to its logged-in UI after a successful
# sign-in. The old code checked once after 1.5s and once after 6s, which reported
# a perfectly good login as "LOGIN INCOMPLETE" on this slow host.
_LOGIN_SETTLE_TIMEOUT = float(os.environ.get("MKWL_LOGIN_SETTLE_TIMEOUT", "120"))

# How old the browser-free engine's session capture may be before a run insists on
# opening a browser (which is the only thing that can refresh it).
_CONTEXT_MAX_AGE_HOURS = float(os.environ.get("MKWL_CONTEXT_MAX_AGE_H", "12"))


def _worker_capture_script(engine: str):
    """The init script a session needs, or None.

    The browser-free engine gets its session context from a dump of the app's own
    worker traffic, and the hook has to wrap Worker BEFORE the app creates one -
    so it must be an init script, decided at launch time. The bridge engine needs
    nothing and pays nothing.
    """
    if engine == "bridge":
        return None
    p = Path(__file__).resolve().parents[1] / "eitaa" / "worker_capture.js"
    return p if p.is_file() else None


def _flood_wait(code: object, wait: object = None) -> int | None:
    """Seconds the server told us to wait, from FLOOD_WAIT_n or an explicit field.

    A short server-declared pause is an instruction to obey, not a reason to
    abandon the remaining recipients -- which is what the old code did.
    """
    for candidate in (wait, code):
        if candidate is None:
            continue
        if isinstance(candidate, (int, float)) and candidate > 0:
            return int(candidate)
        m = re.search(r"(?:FLOOD_WAIT_|WAIT_)(\d+)", str(candidate), re.I)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def _is_limit(detail: str) -> bool:
    low = (detail or "").lower()
    return any(pat in low for pat in _LIMIT_PATTERNS)


#: Engines the panel offers. "hybrid" is the interesting one: it sends with the
#: browser-free engine and keeps the proven page as a per-recipient safety net.
ENGINES = ("bridge", "hybrid", "direct")


def effective_engine(settings: dict) -> str:
    """The engine a job may actually use.

    Single place every job asks, so an unknown or stale value can never route
    work somewhere unexpected. "direct" (pure browser-free, no safety net) still
    needs MKWL_ENABLE_DIRECT=1 because it has no fallback if its session context
    goes stale; "hybrid" is always allowed since it falls back to the bridge.
    """
    engine = str((settings or {}).get("engine", config.ENGINE))
    if engine not in ENGINES:
        return "bridge"
    if engine == "direct" and not config.ENABLE_DIRECT:
        return "hybrid"
    return engine


@dataclass
class Job:
    job_id: str
    kind: str          # "send" | "contacts" | "contacts_save" | "multi"
                       #   | "dryrun" | "session_check" | "photo_export"
    account: str
    stop: bool = False
    task: asyncio.Task | None = None
    started: float = field(default_factory=time.time)
    summary: dict = field(default_factory=dict)
    # Set together with `stop`. Every wait inside a job sleeps on this event
    # instead of a plain timer, so a stop takes effect immediately instead of
    # after the remaining delay.
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def ask_stop(self) -> None:
        self.stop = True
        self.stop_event.set()

    async def wait(self, delay: float) -> bool:
        """Sleep up to `delay`, waking at once if a stop is requested.

        Returns True when the job should stop.
        """
        if self.stop:
            return True
        if delay and delay > 0:
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                return self.stop
        return self.stop


@dataclass
class LoginState:
    """Tracks an in-progress bridge login waiting for the user's code."""
    account: str
    phone: str
    code_future: asyncio.Future
    stage: str = "sending"   # sending | awaiting_code | done


class _NullDriver:
    """Stands in for the browser driver when a job runs without a browser.

    Every browser-only capability answers "not available" instead of blowing up,
    so the ONE send loop can run with or without a page. Anything that really
    needs the page is a clean, reported failure rather than a crash.
    """

    page = None

    async def open(self) -> None:
        return None

    async def is_logged_in(self) -> bool:
        return True  # the direct engine proves this per send

    async def ensure_bridge(self) -> bool:
        return False

    async def bridge_file_ready(self) -> bool:
        return False

    async def _return_to_chat_list(self) -> None:
        return None

    async def bridge_harvest_peers(self, peer_ids):
        return {"ok": False}

    async def bridge_send(self, peer_id, text):
        return {"ok": False, "code": "no browser session in this run"}

    async def bridge_file_send(self, peer_id, caption=""):
        return {"ok": False, "code": "no browser session in this run"}

    async def bridge_file_init(self, path, caption="", locate_timeout=None):
        return {"ok": False, "code": "no browser session in this run"}

    async def send_text(self, name, text, verify=True):
        return SendResult(ok=False, to=name,
                          detail="the browser fallback needs a browser session")

    async def send_file(self, path, caption="", query=""):
        return SendResult(ok=False, to=query,
                          detail="the browser fallback needs a browser session")


class _NullSession:
    """`async with` target for a job that opens no browser."""

    page = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class StageTracker:
    """Live 'what is the bot doing right now' card for a job's setup phase.

    Opening Chromium on this host was measured at 158-203 SECONDS, and the panel
    used to show nothing at all until the first message went out -- so a working
    job and a hung job looked identical. This posts a checklist immediately and
    keeps refreshing it (a heartbeat ticker) so the elapsed time always moves,
    even while a single step is blocked.
    """

    def __init__(self, live, phone: str, steps: list[tuple[str, str]]) -> None:
        self.live = live
        self.phone = phone
        # steps: [(key, label)] in order.
        self.order = [k for k, _ in steps]
        self.labels = dict(steps)
        self.state = {k: "pending" for k in self.order}
        self.took: dict[str, float] = {}
        self.note: str | None = None
        self.start = time.time()
        self._step_start = None
        self._current: str | None = None
        self._ticker: asyncio.Task | None = None
        self._stopped = False

    def _render(self) -> str:
        rows = [(self.labels[k], self.state[k], self.took.get(k)) for k in self.order]
        return cards.live_stages(self.phone, rows, time.time() - self.start, self.note)

    async def _paint(self, force: bool = True) -> None:
        if self.live is None or self._stopped:
            return
        try:
            await self.live.set(self._render(), force=force)
        except Exception:  # noqa: BLE001 - a status card must never break a job
            pass

    async def begin(self, key: str, note: str | None = None) -> None:
        """Mark the previous step done and this one active."""
        now = time.time()
        if self._current and self.state.get(self._current) == "active":
            self.state[self._current] = "done"
            if self._step_start:
                self.took[self._current] = now - self._step_start
        if key in self.state:
            self.state[key] = "active"
        self._current = key
        self._step_start = now
        self.note = note
        await self._paint()
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick())

    async def fail(self, note: str | None = None) -> None:
        if self._current:
            self.state[self._current] = "failed"
        self.note = note
        await self._paint()

    async def _tick(self) -> None:
        """Refresh every 10s so the card visibly moves during a long step."""
        try:
            while not self._stopped:
                await asyncio.sleep(10)
                if self._stopped:
                    return
                await self._paint()
        except asyncio.CancelledError:
            return

    async def done(self, note: str | None = None) -> None:
        """Finish the checklist and stop the heartbeat."""
        if self._current and self.state.get(self._current) == "active":
            self.state[self._current] = "done"
            if self._step_start:
                self.took[self._current] = time.time() - self._step_start
        self.note = note
        await self._paint()
        self.stop()

    def stop(self) -> None:
        self._stopped = True
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None


class AggregateProgress:
    """Shared progress for a SIMULTANEOUS multi-account send.

    Several per-account send jobs run at once but must report into ONE live
    card: the combined "sent of total" across every selected account (their
    contact lists added together), which account most recently sent, and a
    per-account breakdown. Each job calls `update()`; rendering + throttling is
    handled by the LiveCard underneath.
    """

    def __init__(self, live, accounts: list[tuple[str, str]],
                 kind: str | None = None, engine: str | None = None) -> None:
        self.live = live
        self.kind = kind
        self.engine = engine
        self.start = time.time()
        self.order = [acc for acc, _ in accounts]
        self.rows: dict[str, dict] = {
            acc: {"phone": phone, "sent": 0, "failed": 0, "total": 0,
                  "state": "pending"}
            for acc, phone in accounts
        }
        self.current: str | None = None
        self._lock = asyncio.Lock()

    # ---- numbers ----
    def breakdown(self) -> list[dict]:
        return [self.rows[a] for a in self.order if a in self.rows]

    def totals(self) -> tuple[int, int, int]:
        rows = self.breakdown()
        return (sum(r["sent"] for r in rows),
                sum(r["failed"] for r in rows),
                sum(r["total"] for r in rows))

    def _status(self) -> str:
        rows = self.breakdown()
        states = [r["state"] for r in rows]
        if "preparing" in states:
            return "📥 Reading contacts"
        if "running" in states:
            return "🟢 Sending"
        # Only once nothing is in flight can the run be summarized. This check
        # comes BEFORE the limited/stopped ones so a finished run that merely
        # contained a limited account is not reported as "Limited".
        if states and set(states) <= {"done", "failed", "no_targets",
                                      "stopped", "limited"}:
            # Never claim a green "Done" when accounts could not send at all --
            # that made 7 of 8 accounts failing look like a 100% success.
            parts = []
            for count, word in ((states.count("no_targets"), "with no peers"),
                                (states.count("failed"), "failed"),
                                (states.count("limited"), "limited"),
                                (states.count("stopped"), "stopped")):
                if count:
                    parts.append(f"{count} {word}")
            if parts:
                return "⚠️ Done — " + ", ".join(parts)
            return "✅ Done"
        if "limited" in states:
            return "🚫 Limited"
        if "stopped" in states:
            return "🛑 Stopped"
        return "⏳ Starting"

    def render(self) -> str:
        sent, failed, total = self.totals()
        # "Now" means an account is actually working right now; once the run is
        # over it would otherwise keep naming the last account forever.
        busy = any(r["state"] in ("running", "preparing") for r in self.breakdown())
        return cards.live_send_multi(
            self.breakdown(), self.current if busy else None, sent, failed, total,
            time.time() - self.start, status=self._status(),
            engine=self.engine, kind=self.kind,
        )

    # ---- updates ----
    async def update(self, account: str, force: bool = False, **fields) -> None:
        """Merge this account's numbers/state in, then refresh the single card."""
        async with self._lock:
            row = self.rows.get(account)
            if row is None:
                return
            row.update({k: v for k, v in fields.items() if v is not None})
            if row.get("state") == "running":
                self.current = row.get("phone")
        if self.live is not None:
            await self.live.set(self.render(), force=force)

    async def finish(self) -> None:
        """Force one last repaint so the card ends on exact final numbers."""
        if self.live is not None:
            await self.live.set(self.render(), force=True)


class JobManager:
    """Tracks running jobs and enforces one job per account at a time."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._busy: set[str] = set()
        self._logins: dict[str, LoginState] = {}
        # multi-run job id -> the per-account job it is currently waiting on.
        self._multi_children: dict[str, Job] = {}

    def is_busy(self, account: str) -> bool:
        return account in self._busy

    def active_jobs(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.task and not j.task.done()]

    def multi_jobs(self) -> list[Job]:
        """Running multi-account runs (kind "multi")."""
        return [j for j in self.active_jobs() if j.kind == "multi"]

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def stop(self, job_id: str, force: bool = False) -> bool:
        """Ask a job to stop. `force` cancels it outright.

        Graceful stop wakes every wait immediately, so the job ends as soon as
        the message in flight is done. `force` is the second press: it cancels
        the task, which unwinds the browser session through its context manager.
        """
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.ask_stop()
        # Stopping a multi run must also stop the account sending right now,
        # otherwise it would keep going and only the queue would halt.
        child = self._multi_children.get(job_id)
        if child is not None:
            child.ask_stop()
        if force:
            # Cancel the job that actually owns the browser. For a multi run that
            # is the account in flight -- never the sequence itself, otherwise the
            # run would die before it could post its final card.
            if child is not None and child.task and not child.task.done():
                child.task.cancel()
            elif (job.kind != "multi" and job.task and not job.task.done()):
                job.task.cancel()
        return True

    def is_stopping(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        return bool(job and job.stop)

    def stop_multi(self, force: bool = False) -> int:
        """Stop every running multi-account run (and its current account)."""
        return sum(1 for j in self.multi_jobs() if self.stop(j.job_id, force=force))

    def multi_stopping(self) -> bool:
        """True when a multi run has already been asked to stop."""
        return any(j.stop for j in self.multi_jobs())

    def stop_account(self, account: str, force: bool = False) -> int:
        n = 0
        for job in list(self._jobs.values()):
            if job.account == account and job.task and not job.task.done():
                self.stop(job.job_id, force=force)
                n += 1
        return n

    def account_stopping(self, account: str) -> bool:
        return any(j.account == account and j.stop and j.task and not j.task.done()
                   for j in self._jobs.values())

    def _new_job(self, kind: str, account: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex[:8], kind=kind, account=account)
        self._jobs[job.job_id] = job
        return job

    async def run_send(self, account: str, content: dict, settings: dict,
                       report: Report, recipients: list[str] | None = None,
                       live=None, account_phone: str | None = None,
                       agg: AggregateProgress | None = None) -> Job:
        """Start a send job, honouring the engine setting.

        engine=direct -> browser-free MTProto (no Chromium at all), which needs
        saved peers for the targets. engine=bridge -> the proven tweb path.
        Routing mirrors run_contacts, which already dispatched on the engine.
        """
        job = self._new_job("send", account)
        engine = effective_engine(settings)
        if engine == "direct":
            job.task = asyncio.create_task(
                self._send_job_direct(job, content, settings, report,
                                      recipients, live, account_phone, agg)
            )
        else:
            job.task = asyncio.create_task(
                self._send_job(job, content, settings, report, recipients,
                               live, account_phone, agg)
            )
        return job

    async def run_send_multi(self, accounts: list[tuple[str, str]], content: dict,
                             settings: dict, report: Report, live=None) -> Job:
        """Send from several accounts ONE AFTER ANOTHER, in the order given.

        `accounts` is [(account, phone)] in the order the owner ticked them, so
        the first account ticked is the first to send. An account runs until it
        finishes, is stopped, or fails for ANY reason (expired session, limit,
        crash) -- then the run moves on to the next one. Nothing runs in
        parallel, so only one browser is open at a time.

        Returns the single "multi" job that represents the whole run; stopping it
        stops the account currently sending and skips the rest.
        """
        multi = self._new_job("multi", "*")
        multi.task = asyncio.create_task(
            self._multi_send_sequence(multi, list(accounts), content, settings,
                                      report, live))
        return multi

    async def _multi_send_sequence(self, multi: Job, accounts: list[tuple[str, str]],
                                   content: dict, settings: dict, report: Report,
                                   live=None) -> None:
        kind = "File" if content.get("kind") == "file" else "Text"
        engine = effective_engine(settings)
        agg = AggregateProgress(live, accounts, kind=kind, engine=engine)
        start = time.time()

        # ---- PHASE 1: know the reach before sending anything ----------------
        # Every account's contacts are counted up front (collected first if this
        # account has none saved yet), so the card can show a real grand total
        # instead of a number that grows as the run goes.
        for acc, phone in accounts:
            if multi.stop:
                break
            known = contacts_store.count(acc)
            if not known:
                await agg.update(acc, state="preparing", force=True)
                save_job = await self.run_save_contacts(acc, report, phone)
                self._multi_children[multi.job_id] = save_job
                try:
                    await save_job.task
                except asyncio.CancelledError:
                    multi.ask_stop()
                except Exception:  # noqa: BLE001 - a failure here just means 0
                    pass
                known = contacts_store.count(acc)
            await agg.update(acc, total=known, state="pending", force=True)

        _, _, grand_total = agg.totals()
        await report(cards.multi_ready(agg.breakdown(), grand_total, kind=kind))

        # ---- PHASE 2: send, one account at a time, in order -----------------
        order = 0
        for acc, phone in accounts:
            if multi.stop:
                break
            order += 1
            await agg.update(acc, state="running", force=True)
            job = await self.run_send(acc, content, settings, report, live=None,
                                      account_phone=phone, agg=agg)
            self._multi_children[multi.job_id] = job
            try:
                await job.task
            except asyncio.CancelledError:
                # This account was force-stopped. Only IT was cancelled, so the
                # sequence stays alive to report and (if asked) stop cleanly.
                await agg.update(acc, state="stopped", force=True)
            except Exception as exc:  # noqa: BLE001 - never abort the whole run
                await report(cards.error_card(
                    "multi_send", acc, code=type(exc).__name__, detail=str(exc),
                    phase="account_job", trace_id=multi.job_id))

            # Whatever happened, record an outcome for this account and move on.
            row = agg.rows.get(acc, {})
            summary = job.summary or {}
            if row.get("state") not in ("done", "stopped", "limited", "failed",
                                        "no_targets"):
                # The job returned without reporting (e.g. it was not logged in),
                # so mark it failed rather than leaving it looking pending.
                await agg.update(acc, state="failed", force=True)
                row = agg.rows.get(acc, {})
            await report(cards.multi_account_done(
                phone, order, len(accounts), row.get("state", "failed"),
                int(summary.get("sent", row.get("sent", 0)) or 0),
                int(summary.get("failed", row.get("failed", 0)) or 0),
                int(row.get("total", 0) or 0),
                next_phone=(accounts[order][1] if order < len(accounts)
                            and not multi.stop else None)))

        self._multi_children.pop(multi.job_id, None)
        await agg.finish()
        sent, failed, total = agg.totals()
        multi.summary = {"sent": sent, "failed": failed, "total": total,
                         "accounts": len(accounts)}
        await report(cards.multi_send_finished(
            agg.breakdown(), sent, failed, total, time.time() - start,
            kind=kind, engine=engine, stopped=multi.stop))

    async def _send_progress(self, live, agg: AggregateProgress | None,
                             account: str, phone: str, *, sent: int, failed: int,
                             total: int, elapsed: float, status: str, state: str,
                             engine: str | None, kind: str | None,
                             force: bool = False) -> None:
        """Publish progress to whichever card this job owns.

        A multi-account job feeds the shared aggregate card; a single-account
        job keeps its own live card exactly as before.
        """
        if agg is not None:
            await agg.update(account, sent=sent, failed=failed, total=total,
                             state=state, force=force)
        elif live is not None:
            await live.set(cards.live_send(phone, sent, failed, total, elapsed,
                                           status=status, engine=engine, kind=kind),
                           force=force)

    @staticmethod
    def _can_run_browserless(engine: str, account: str, recipients, settings) -> bool:
        """Can this run skip Chromium entirely?

        Only when ALL of these hold, because there is no page to fall back to:
          * the engine is not the bridge
          * the direct engine's session context is present
          * the recipients come from the saved contacts (not names passed in)
          * every one of them carries an access_hash, so no peer needs the page
        Otherwise the job opens a browser exactly as before.
        """
        if engine == "bridge" or recipients is not None:
            return False
        # OFF unless the owner explicitly asks for it. A browser-free run has no
        # page, so "hybrid" loses its per-recipient safety net - exactly the
        # no-fallback situation MKWL_ENABLE_DIRECT exists to gate. Opt-in only.
        if not bool((settings or {}).get("browserless", False)):
            return False
        ctx_age = direct_ctx.newest_capture_age_hours(account)
        if not direct_ctx.has_context(account):
            return False
        # A stale capture cannot be refreshed without a page, so every send would
        # fail until the brake stopped the run. Take the browser instead.
        if ctx_age is None or ctx_age > _CONTEXT_MAX_AGE_HOURS:
            print(f"[engine] context for {account} is {ctx_age}h old; taking the "
                  f"browser so it can be refreshed", flush=True)
            return False
        saved = contacts_store.contacts(account)
        if not saved:
            return False
        with_hash = sum(1 for c in saved if c.get("access_hash") and c.get("peer_id"))
        if with_hash != len(saved):
            # Some contacts would need the page; take the browser to be safe.
            return False
        return True

    @staticmethod
    def settings_provider() -> dict:
        """Current panel settings, without importing the panel at module level."""
        try:
            from bot.store import store as _store
            return dict(_store.settings)
        except Exception:  # noqa: BLE001
            return {}

    async def run_save_contacts(self, account: str, report: Report,
                                account_phone: str | None = None) -> Job:
        """Collect the account's contacts ONCE and cache them.

        This is the slow part of any send (Eitaa's contact list is virtualized,
        so it has to be scrolled). Doing it here means every later send starts
        delivering immediately. Peers are harvested in the same pass, so the
        browser-free sender keeps working if it is ever re-enabled.
        """
        job = self._new_job("contacts_save", account)
        job.task = asyncio.create_task(
            self._save_contacts_job(job, report, account_phone or account))
        return job

    async def run_photo_export(self, account: str, report: Report,
                               account_phone: str | None = None,
                               live=None, direction: str = "both",
                               send_document=None) -> Job:
        """Export this account's photos to PDF, one photo per page.

        Read-only on Eitaa: it walks the private chats, searches each for photos,
        downloads them and renders the pages. Nothing is sent to anybody there.

        The work lives in the isolated `photo_export/` package and is imported
        inside the job, so a missing or broken package costs one error card and
        leaves every other job untouched.
        """
        job = self._new_job("photo_export", account)
        job.task = asyncio.create_task(
            self._photo_export_job(job, report, account_phone or account,
                                   live, direction, send_document))
        return job

    async def _photo_export_job(self, job: Job, report: Report, phone: str,
                                live=None, direction: str = "both",
                                send_document=None) -> None:
        account = job.account
        self._busy.add(account)
        try:
            from photo_export import cards as px_cards
            from photo_export import engine as px_engine

            engine_name = effective_engine(self.settings_provider())
            async with session_pool.lease(
                    account, headed=config.HEADED_JOBS,
                    init_script_path=_worker_capture_script(engine_name)) as session:
                driver = EitaaDriver(session)
                await driver.open()
                if not await driver.is_logged_in():
                    await report(cards.error_card(
                        "photo_export", account, code="not_logged_in",
                        detail="account is not logged in"))
                    return

                res = await px_engine.export(
                    driver, account, phone, direction=direction,
                    report=report, live=live, send_document=send_document,
                    should_stop=lambda: job.stop)

            if not res.get("ok"):
                await report(cards.error_card(
                    "photo_export", account, code=str(res.get("code")),
                    detail=str(res.get("detail") or res.get("code")),
                    trace_id=job.job_id))
                return
            if res.get("nothing_found") or not res.get("photos"):
                await report(px_cards.nothing_found(
                    account=account, phone=phone, direction=direction,
                    chats_total=res.get("chats_total") or 0,
                    elapsed=res.get("elapsed") or 0.0))
                return

            job.summary = {"photos": res.get("photos"),
                           "files": len(res.get("files") or [])}
            await report(px_cards.finished(
                account=account, phone=phone, direction=direction,
                photos=res.get("photos") or 0,
                sent_by_me=res.get("sent_by_me") or 0,
                received=res.get("received") or 0,
                chats_with_photos=res.get("chats_with_photos") or 0,
                chats_total=res.get("chats_total") or 0,
                files=res.get("files") or [],
                elapsed=res.get("elapsed") or 0.0,
                skipped=res.get("skipped") or 0,
                stopped=bool(res.get("stopped"))))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("photo_export", account,
                                          code=type(exc).__name__,
                                          detail=str(exc), phase="export",
                                          trace_id=job.job_id))
        finally:
            self._busy.discard(account)
            flush = getattr(live, "flush", None)
            if flush is not None:
                try:
                    await flush()
                except Exception:  # noqa: BLE001
                    pass

    async def run_session_check(self, account: str, report: Report,
                                account_phone: str | None = None,
                                live=None) -> Job:
        """Verify the account's Eitaa session is still usable, without sending.

        Every job below already starts with `driver.is_logged_in()` and aborts
        with a not_logged_in card - but that was only discoverable by starting a
        real run. This exposes the same gate on its own.

        The check lives in the isolated `session_check/` package; it is imported
        inside the job so a missing or broken package costs one error card and
        leaves every other job untouched.
        """
        job = self._new_job("session_check", account)
        job.task = asyncio.create_task(
            self._session_check_job(job, report, account_phone or account, live))
        return job

    async def _session_check_job(self, job: Job, report: Report, phone: str,
                                 live=None) -> None:
        account = job.account
        self._busy.add(account)
        try:
            from session_check.checker import check_session
            job.summary = await check_session(
                account, phone, report, live=live,
                engine=effective_engine(self.settings_provider()))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("session_check", account,
                                          code=type(exc).__name__, detail=str(exc),
                                          phase="check", trace_id=job.job_id))
        finally:
            self._busy.discard(account)

    async def _build_transport(self, engine: str, driver, account: str,
                               report: Report, stages=None):
        """Return (transport, engine_actually_used).

        Falls back to the bridge - loudly, never silently - when the browser-free
        engine cannot be set up, because a campaign that silently changes engine
        is impossible to reason about later.
        """
        bridge = transports.BridgeTransport(driver)
        if engine == "bridge":
            return bridge, "bridge"

        # The direct engine needs a live session context; refresh it from the
        # page we already have open, which is exactly what used to be missing.
        refresh = await direct_ctx.refresh_from_driver(driver, account)
        if not refresh.get("ok") and not direct_ctx.has_context(account):
            await report(cards.error_card(
                "engine", account, code="direct_context_missing",
                detail=f"{refresh.get('code')} - sending with the bridge instead"))
            return bridge, "bridge"
        if refresh.get("ok"):
            print(f"[engine] direct context refreshed: {refresh.get('kept')} "
                  f"record(s), user_id={refresh.get('user_id')}", flush=True)

        try:
            from direct.sender import DirectSender
            sender = await asyncio.to_thread(DirectSender, account)
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card(
                "engine", account, code="direct_unavailable",
                detail=f"{type(exc).__name__}: {str(exc)[:160]} - "
                       f"sending with the bridge instead"))
            return bridge, "bridge"

        hashes = transports.access_hash_map(contacts_store.contacts(account))
        direct = transports.DirectTransport(sender, hashes)
        print(f"[engine] direct ready: {len(hashes)} peer(s) addressable without a "
              f"browser", flush=True)
        if engine == "direct":
            return direct, "direct"
        return transports.HybridTransport(direct, bridge), "hybrid"

    async def run_dry_run(self, account: str, content: dict, settings: dict,
                          report: Report, account_phone: str | None = None,
                          live=None) -> Job:
        """Send the current content ONCE, to the account's own Saved Messages.

        The whole pipeline gets exercised - engine, upload, caption - against a
        recipient that is the owner themselves. Before this, the only way to find
        out that a caption was wrong or a file would not upload was to discover it
        halfway through a campaign.
        """
        job = self._new_job("dryrun", account)
        job.task = asyncio.create_task(
            self._dry_run_job(job, content, settings, report,
                              account_phone or account, live))
        return job

    async def _dry_run_job(self, job: Job, content: dict, settings: dict,
                           report: Report, phone: str, live=None) -> None:
        account = job.account
        engine = effective_engine(settings)
        is_file = content.get("kind") == "file"
        self._busy.add(account)
        start = time.time()
        stages = StageTracker(live, phone, [
            ("browser", "open browser"),
            ("login", "check login"),
            ("upload", "upload file" if is_file else "prepare text bridge"),
            ("send", "send to Saved Messages"),
        ]) if live is not None else None
        try:
            if stages is not None:
                await stages.begin("browser", "Test send: only you receive this.")
            async with session_pool.lease(account, headed=config.HEADED_JOBS,
                                   init_script_path=_worker_capture_script(engine)
                                   ) as session:
                driver = EitaaDriver(session)
                await driver.open()
                if stages is not None:
                    await stages.begin("login")
                if not await driver.is_logged_in():
                    if stages is not None:
                        await stages.fail("This account is not logged in.")
                    await report(cards.error_card("dry_run", account,
                                                  code="not_logged_in",
                                                  detail="account is not logged in"))
                    return
                transport, engine = await self._build_transport(
                    engine, driver, account, report, stages)
                # Saved Messages = the account's own peer, which every engine can
                # address without any contact relationship, so a PEER_FLOOD here
                # would mean something genuinely wrong with the account.
                # The browser-free engines address Saved Messages through the
                # capture's own self-peer, so the literal "self" is resolved by
                # the transport; only the page needs a numeric id.
                self_peer = ("self" if getattr(transport, "browserless", False)
                             or isinstance(transport, transports.HybridTransport)
                             else await driver.self_peer_id())
                if not self_peer:
                    await report(cards.error_card(
                        "dry_run", account, code="no_self_peer",
                        detail="could not resolve this account's own Saved Messages"))
                    return
                if stages is not None:
                    await stages.begin("upload")
                if is_file:
                    finit = await transport.prepare_file(
                        content.get("file_path", ""), content.get("caption", ""))
                    if not finit.get("ok"):
                        if stages is not None:
                            await stages.fail(str(finit.get("code"))[:120])
                        await report(cards.error_card(
                            "dry_run", account, code="upload_failed",
                            detail=str(finit.get("code"))))
                        return
                else:
                    await driver.ensure_bridge()
                if stages is not None:
                    await stages.begin("send")
                t0 = time.time()
                if is_file:
                    res = await transport.send_file(self_peer, content.get("caption", ""))
                else:
                    res = await transport.send_text(self_peer, content.get("text", ""))
                took = time.time() - t0
                if stages is not None:
                    await stages.done("Delivered to your Saved Messages."
                                      if res.get("ok") else "Failed - see the card.")
                job.summary = {"ok": bool(res.get("ok")), "engine": engine,
                               "seconds": round(took, 2)}
                await report(cards.dry_run_card(
                    phone, engine, "File" if is_file else "Text",
                    bool(res.get("ok")), str(res.get("code") or res.get("method") or ""),
                    took, time.time() - start))
                try:
                    await transport.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            if stages is not None:
                await stages.fail(f"{type(exc).__name__}: {str(exc)[:120]}")
            await report(cards.error_card("dry_run", account,
                                          code=type(exc).__name__, detail=str(exc),
                                          trace_id=job.job_id))
        finally:
            if stages is not None:
                stages.stop()
            flush = getattr(live, "flush", None)
            if flush is not None:
                try:
                    await flush()
                except Exception:  # noqa: BLE001
                    pass
            self._busy.discard(account)

    async def _collect_contacts(self, driver, account: str,
                                should_stop: Callable[[], bool] | None = None,
                                report: Report | None = None) -> tuple[list[dict], str]:
        """Get an account's contacts the fast way, with the old way as backup.

        Returns (contacts, source). `source` is "api" or "scroll".

        The API path asks Eitaa for the whole contact list in one call and gets
        id + access_hash for every entry. Measured live on one account: 1,094
        contacts in 4 seconds, versus the DOM scroll which was still unfinished
        after 10 minutes AND is capped by max_scrolls, so long lists came back
        truncated (that is what produced "saved 1,190" for an account Eitaa
        reported 6,436 entries for).
        """
        try:
            res = await driver.bridge_contacts_list()
        except Exception:  # noqa: BLE001
            res = None
        if res and res.get("ok"):
            items = res.get("contacts") or []
            print(f"[contacts] api list: {len(items)} contacts "
                  f"(skipped {res.get('skipped', 0)} of {res.get('raw', 0)})", flush=True)
            # An empty-but-successful answer means this account genuinely has no
            # contacts. Scrolling the DOM for 10 minutes cannot invent any, so
            # take the answer instead of falling back.
            return items, "api"

        code = (res or {}).get("code") if isinstance(res, dict) else "bridge_unavailable"
        print(f"[contacts] api list unavailable ({code}); falling back to the "
              f"slow DOM scroll", flush=True)
        if report is not None:
            await report(cards.error_card(
                "contacts_list", account, code="api_list_unavailable",
                detail=f"{code} - using the slow scroll fallback for this run"))
        contacts = await driver.collect_all_contacts(should_stop=should_stop)
        try:
            await driver._return_to_chat_list()
        except Exception:  # noqa: BLE001
            pass
        return contacts, "scroll"

    async def _save_contacts_job(self, job: Job, report: Report, phone: str) -> None:
        account = job.account
        self._busy.add(account)
        start = time.time()
        before = contacts_store.count(account)
        # If the browser-free engine is selected, grab its session context while
        # this browser is open. That way "Update Contacts" alone is enough to make
        # hybrid ready -- the owner never has to run a send just to arm it.
        engine = effective_engine(self.settings_provider())
        try:
            async with session_pool.lease(account, headed=config.HEADED_JOBS,
                                    init_script_path=_worker_capture_script(engine)
                                    ) as session:
                driver = EitaaDriver(session)
                await driver.open()
                if not await driver.is_logged_in():
                    await report(cards.error_card(
                        "save_contacts", account, code="not_logged_in",
                        detail="account is not logged in"))
                    return

                contacts, source = await self._collect_contacts(
                    driver, account, should_stop=lambda: job.stop, report=report)
                record = contacts_store.save(account, contacts)
                print(f"[contacts] saved {record['count']} for {account} "
                      f"(source={source})", flush=True)
                if job.stop and source == "scroll":
                    await report(cards.contacts_saved(
                        phone, record["count"],
                        sum(1 for c in record["contacts"] if c.get("peer_id")),
                        time.time() - start, replaced=before, partial=True))
                    return

                peer_ids = [c.get("peer_id") for c in record["contacts"]
                            if c.get("peer_id")]
                # Harvesting is a bonus for the browser-free engine; it never
                # affects the saved list.
                await self._harvest_peers(driver, account, report, peer_ids)

                job.summary = {"contacts": record["count"],
                               "with_peer_id": len(peer_ids)}
                await report(cards.contacts_saved(
                    phone, record["count"], len(peer_ids),
                    time.time() - start, replaced=before))

                if engine != "bridge":
                    ref = await direct_ctx.refresh_from_driver(driver, account)
                    print(f"[engine] context refresh after contacts: {ref}", flush=True)
                    await report(cards.card(
                        "⚡ BROWSER-FREE ENGINE" if ref.get("ok")
                        else "⚠️ BROWSER-FREE ENGINE NOT READY",
                        [("Phone  ", phone),
                         ("Status ", "session captured, ready to send without a browser"
                                     if ref.get("ok") else str(ref.get("code"))[:120]),
                         ("Records", ref.get("kept") or None)],
                        footer=("Sends can now go straight over HTTPS; the browser stays "
                                "as the per-recipient safety net."
                                if ref.get("ok") else
                                "Sends will use the browser path until this succeeds.")))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("save_contacts", account,
                                          code=type(exc).__name__, detail=str(exc),
                                          phase="collect", trace_id=job.job_id))
        finally:
            self._busy.discard(account)

    async def run_contacts(self, account: str, prefix: str, count: int,
                           settings: dict, report: Report, live=None,
                           account_phone: str | None = None) -> Job:
        job = self._new_job("contacts", account)
        engine = effective_engine(settings)
        phone = account_phone or account
        if engine == "direct":
            job.task = asyncio.create_task(
                self._contacts_job_direct(job, prefix, count, settings, report, live, phone)
            )
        else:
            job.task = asyncio.create_task(
                self._contacts_job(job, prefix, count, settings, report, live, phone)
            )
        return job

    # ---- bridge login (no noVNC: phone + code in Telegram) ----
    async def start_bridge_login(self, account: str, phone: str, report: Report,
                                 live=None) -> bool:
        """Begin a no-noVNC login: send the code, then wait for the user's code.

        Returns False if the account is busy. The code is delivered later via
        submit_login_code(). `live` is an optional card that shows the stages,
        because opening the browser alone takes minutes on a weak host and the
        login used to be completely silent until the code arrived.
        """
        if account in self._busy:
            return False
        self._busy.add(account)
        asyncio.create_task(self._bridge_login_job(account, phone, report, live))
        return True

    def login_stage(self, account: str) -> str | None:
        st = self._logins.get(account)
        return st.stage if st else None

    def submit_login_code(self, account: str, code: str) -> str:
        """Hand a received code to a waiting bridge-login job."""
        st = self._logins.get(account)
        if not st:
            return "no_pending"
        if st.stage != "awaiting_code":
            return "not_ready"
        if st.code_future.done():
            return "already"
        st.code_future.set_result(code)
        return "ok"

    async def _wait_logged_in(self, driver, session, stages=None,
                              timeout: float = _LOGIN_SETTLE_TIMEOUT) -> bool:
        """Poll until the app really shows the logged-in UI.

        Returns True as soon as it does. Reloads the page once at the halfway
        point, because a stuck SPA boot is fixed by a reload while a slow one is
        only made worse by it.
        """
        deadline = time.time() + timeout
        reloaded = False
        checks = 0
        while time.time() < deadline:
            checks += 1
            try:
                if await driver.is_logged_in():
                    print(f"[login] app is logged in after {checks} check(s), "
                          f"{time.time() - (deadline - timeout):.0f}s", flush=True)
                    return True
            except Exception:  # noqa: BLE001
                pass
            left = deadline - time.time()
            if not reloaded and left < timeout / 2:
                reloaded = True
                if stages is not None:
                    await stages.begin("finalize", "Still booting - reloading the page "
                                                   "once and continuing to wait.")
                try:
                    await session.page.reload(wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(3)
        print(f"[login] app never showed the logged-in UI within {timeout:.0f}s "
              f"({checks} checks)", flush=True)
        return False

    async def _bridge_login_job(self, account: str, phone: str, report: Report,
                                live=None) -> None:
        from capture.browser import open_session  # noqa: F401 - legacy local import
        from eitaa.driver import EitaaDriver
        from eitaa.login_flow import (
            normalize_phone_intl, resolve_api_creds, send_code, sign_in,
        )

        state = LoginState(account, phone, asyncio.get_event_loop().create_future())
        self._logins[account] = state
        api_id, api_hash = resolve_api_creds()
        intl = normalize_phone_intl(phone)
        stages = StageTracker(live, re.sub(r"\D", "", intl), [
            ("browser", "open browser"),
            ("app", "load Eitaa web"),
            ("code", "request login code"),
            ("signin", "sign in with your code"),
            ("finalize", "wait for the app to log in"),
            ("contacts", "read contacts"),
        ]) if live is not None else None
        try:
            if stages is not None:
                await stages.begin("browser", "Chromium takes 2-3 minutes on this "
                                              "server. This card keeps ticking.")
            async with session_pool.lease(account, headed=config.HEADED_JOBS) as session:
                driver = EitaaDriver(session)
                # NOTE: driver.open() navigates to Eitaa itself. This used to
                # call session.goto() first as well, so the whole web app was
                # downloaded TWICE on a link where every request costs 1-3s.
                if stages is not None:
                    await stages.begin("app")
                await driver.open()

                if await driver.is_logged_in():
                    if stages is not None:
                        await stages.done("Already logged in.")
                    await report(cards.card(
                        "👤 ACCOUNT READY",
                        [("Account", account), ("Status", "already logged in")]))
                    return

                if stages is not None:
                    await stages.begin("code")
                sc = await send_code(driver, intl, api_id, api_hash)
                if not sc.get("ok"):
                    code = str(sc.get("code", ""))
                    if stages is not None:
                        await stages.fail(f"the server refused to send a code: {code[:60]}")
                        stages.stop()
                    if "FLOOD" in code.upper():
                        await report(cards.card(
                            "🚫 RATE LIMITED",
                            [("Account", account), ("Phone", intl), ("Server", code)],
                            footer="Too many code requests. Wait the stated time, don't retry now."))
                    else:
                        await report(cards.error_card(
                            "login_sendcode", account, code="sendCode", detail=code))
                    return

                phch = sc.get("phone_code_hash")
                if not phch:
                    await report(cards.error_card(
                        "login_sendcode", account, code="no_hash",
                        detail="server returned no phone_code_hash"))
                    return

                state.stage = "awaiting_code"
                if stages is not None:
                    await stages.begin("signin", "Waiting for the code you received. "
                                                 "Send it here as digits.")
                await report(cards.card(
                    "📩 CODE SENT",
                    [("Account", account), ("Phone", intl)],
                    footer="Send me the login code here (digits only). "
                           "Never share your code anywhere else."))

                try:
                    code = await asyncio.wait_for(state.code_future, timeout=300)
                except asyncio.TimeoutError:
                    await report(cards.card(
                        "⌛ LOGIN TIMEOUT",
                        [("Account", account)],
                        footer="No code received in time. Tap Add Account to try again."))
                    return

                si = await sign_in(driver, intl, phch, code)
                if si.get("needs_password"):
                    await report(cards.card(
                        "🔐 2FA REQUIRED",
                        [("Account", account)],
                        footer="This account has a login password (2FA). Add it via the "
                               "noVNC button for now, or ask to build 2FA."))
                    return
                if not si.get("ok"):
                    await report(cards.error_card(
                        "login_signin", account, code="signIn", detail=str(si.get("code"))))
                    return

                # finalize (setUserAuth) already ran in-page. Confirming this used
                # to be one check after 1.5s, then a reload and ONE check after
                # 6s -- and on this host that reported "LOGIN INCOMPLETE" for a
                # login that had actually succeeded, because booting the app after
                # sign-in takes longer than 6 seconds here. Now it polls, reloads
                # once in the middle, and says how long it waited.
                if stages is not None:
                    await stages.begin("finalize", "Sign-in accepted. Waiting for the "
                                                   "app to switch to the chat list.")
                logged = await self._wait_logged_in(driver, session, stages)

                if logged:
                    # Save the contacts list RIGHT NOW, while the browser is
                    # already open and logged in, so the owner never has to
                    # trigger it by hand and the first send starts delivering
                    # immediately.
                    contacts = pvs = None
                    t_save = time.time()
                    if stages is not None:
                        await stages.begin("contacts")
                    try:
                        collected, source = await self._collect_contacts(
                            driver, account, report=report)
                        record = contacts_store.save(account, collected)
                        peer_ids = [c.get("peer_id") for c in record["contacts"]
                                    if c.get("peer_id")]
                        print(f"[contacts] saved {record['count']} at login for "
                              f"{account} (source={source})", flush=True)
                        # The saved list IS the account's contact count, so no
                        # extra stats call is needed for it.
                        contacts = record["count"]
                        await report(cards.contacts_saved(
                            re.sub(r"\D", "", intl), record["count"],
                            len(peer_ids), time.time() - t_save))
                        # Peer harvesting only feeds the browser-free engine, so
                        # it must NOT delay the ACCOUNT ADDED card: it walks
                        # every peer in batches and on a slow host that is
                        # minutes of extra waiting for something optional.
                        await self._harvest_peers(driver, account, report, peer_ids)
                    except Exception as exc:  # noqa: BLE001 - login still succeeded
                        await report(cards.error_card(
                            "save_contacts", account, code=type(exc).__name__,
                            detail=str(exc), phase="on_add",
                            trace_id="login"))
                    if contacts is None:
                        # Contacts-only stats: the PV count pages getDialogs and
                        # cost 98s live, which is not worth blocking a login for.
                        try:
                            s = await driver.bridge_stats(with_pvs=False)
                            if s:
                                contacts = s.get("contacts")
                        except Exception:  # noqa: BLE001
                            pass
                    phone_digits = re.sub(r"\D", "", intl)
                    try:
                        from bot.store import store as _store
                        _store.set_account_meta(account, phone=phone_digits,
                                                contacts=contacts, pvs=pvs)
                        engine = _store.engine
                    except Exception:  # noqa: BLE001
                        engine = None
                    if stages is not None:
                        await stages.done(
                            f"Ready: {contacts_store.count(account):,} contacts saved.")
                    await report(cards.account_added(
                        account, phone_digits, contacts, pvs, engine,
                        saved=contacts_store.count(account)))
                else:
                    if stages is not None:
                        await stages.fail(
                            f"The app did not reach the chat list within "
                            f"{int(_LOGIN_SETTLE_TIMEOUT)}s.")
                        stages.stop()
                    # The session usually IS valid at this point -- only the UI
                    # never settled -- so say what to do instead of implying the
                    # login failed.
                    await report(cards.card(
                        "⚠️ LOGIN NOT CONFIRMED",
                        [("Account", account),
                         ("Waited ", f"{int(_LOGIN_SETTLE_TIMEOUT)}s after sign-in")],
                        footer="The code was accepted but the app never showed the chat "
                               "list in time, which on a slow server usually means it is "
                               "still booting. The session is probably fine: open the "
                               "account and tap '🔄 Update Contacts' -- if that reads "
                               "your contacts, the login worked and you can send. "
                               "Otherwise add the account again."))
        except Exception as exc:  # noqa: BLE001
            if stages is not None:
                await stages.fail(f"{type(exc).__name__}: {str(exc)[:120]}")
            await report(cards.error_card(
                "bridge_login", account, code=type(exc).__name__, detail=str(exc)))
        finally:
            if stages is not None:
                stages.stop()
            self._logins.pop(account, None)
            self._busy.discard(account)

    # ---- send job ----
    async def _send_job(self, job: Job, content: dict, settings: dict,
                        report: Report, recipients: list[str] | None,
                        live=None, account_phone: str | None = None,
                        agg: AggregateProgress | None = None) -> None:
        account = job.account
        phone = account_phone or account
        # This job always opens a browser session (it is what reads contacts and
        # what the safety net needs), but WHO actually delivers each message is
        # decided by the transport built below: the page (bridge), plain HTTPS
        # with no browser (direct), or direct-with-page-fallback (hybrid).
        engine = effective_engine(settings)
        kind = "File" if content.get("kind") == "file" else "Text"
        delay = float(settings.get("text_send_delay", config.TEXT_SEND_DELAY))
        log_every = int(settings.get("send_log_every", config.SEND_LOG_EVERY))
        try:
            conc = int(settings.get("send_concurrency", config.SEND_CONCURRENCY))
        except (TypeError, ValueError):
            conc = 1
        conc = max(1, min(10, conc))
        stop_on_limit = bool(settings.get("stop_on_limit", config.STOP_ON_LIMIT))
        self._busy.add(account)
        start = time.time()
        sent = failed = skipped = 0
        # Declared out here so the finally block can always persist it: a Force
        # Stop cancels this task, and a CancelledError skips every flush inside
        # the body (measured: 11 delivered, 0 recorded -> 11 duplicates).
        ledger = None
        transport = None
        blocked = None
        # Live checklist so the panel shows what is happening from the first
        # second. Only for a single-account run: a multi-account run already owns
        # the shared card.
        # THE BIG ONE: when the browser-free engine is armed and the contacts are
        # already cached with their access_hash, this run needs no browser at all.
        # Opening Chromium on this host costs 158-203 SECONDS and ~500 MB before a
        # single message moves, and a busy renderer is what froze earlier runs.
        browserless = self._can_run_browserless(engine, account, recipients, settings)
        stages = None
        if live is not None and agg is None:
            stages = StageTracker(live, phone, [
                ("browser", "connect (no browser needed)" if browserless
                            else "open browser"),
                ("login", "check session"),
                ("contacts", "read contacts"),
                ("upload", "upload file" if content.get("kind") == "file"
                           else "prepare send path"),
                ("send", "deliver messages"),
            ])
        try:
            if stages is not None:
                await stages.begin(
                    "browser",
                    "Browser-free run: sending starts in seconds." if browserless
                    else "Chromium takes 2-3 minutes on this server. This card keeps "
                         "ticking while it starts.")
            async with (_NullSession() if browserless else session_pool.lease(
                    account, headed=config.HEADED_JOBS,
                    init_script_path=_worker_capture_script(engine))) as session:
                driver = _NullDriver() if browserless else EitaaDriver(session)
                await driver.open()
                if stages is not None:
                    await stages.begin("login")
                if not await driver.is_logged_in():
                    if stages is not None:
                        await stages.fail("This account is not logged in.")
                        stages.stop()
                    await report(cards.error_card("send", account, code="not_logged_in",
                                                  detail="account is not logged in"))
                    return

                is_file = content.get("kind") == "file"
                if stages is not None:
                    await stages.begin("contacts")
                if recipients is None:
                    # Prefer the SAVED contacts list: collecting it means
                    # scrolling Eitaa's virtualized list for minutes, and it was
                    # being redone on every single send. Saved once, sends start
                    # immediately.
                    recipient_items = contacts_store.items(account)
                    if recipient_items:
                        print(f"[send] using {len(recipient_items)} saved contacts "
                              f"(no re-collect)", flush=True)
                    else:
                        # Keep BOTH title and peer_id: peer_id drives the fast
                        # bridge send (Eitaa's own engine, no UI), title is the
                        # search fallback + failure label.
                        t_col = time.time()
                        contacts, source = await self._collect_contacts(
                            driver, account, should_stop=lambda: job.stop,
                            report=report)
                        contacts_store.save(account, contacts)
                        recipient_items = contacts_store.items(account)
                        await report(cards.contacts_saved(
                            phone, len(recipient_items),
                            sum(1 for _, p in recipient_items if p),
                            time.time() - t_col))
                else:
                    # Externally-supplied recipients are plain names -> no peer_id,
                    # so they use the proven UI flow unchanged.
                    recipient_items = [(name, None) for name in recipients]
                # Resume support: skip recipients that already received THIS
                # exact content. Changing the text/file/caption starts a fresh
                # ledger, so a new message always goes to everyone.
                ledger = progress_store.open_ledger(
                    account, progress_store.content_key(content))
                if ledger.done:
                    all_items = recipient_items
                    recipient_items = [(n, p) for (n, p) in all_items
                                       if not ledger.has(n, p)]
                    skipped = len(all_items) - len(recipient_items)
                    if skipped:
                        print(f"[send] resuming: {skipped} recipient(s) already got "
                              f"this content, {len(recipient_items)} left", flush=True)
                        await report(cards.card(
                            "↩️ RESUMING SEND",
                            [("Phone     ", phone),
                             ("Already   ", f"{skipped:,} delivered earlier"),
                             ("Remaining ", f"{len(recipient_items):,}")],
                            footer="Continuing where the previous run stopped, so nobody "
                                   "gets this message twice. Change the content to start "
                                   "a fresh run for everyone."))

                # Skip peers Eitaa has already refused permanently. Measured
                # live: half of one account's contacts answered PEER_FLOOD every
                # single time, so retrying them doubled every run's length and
                # produced hundreds of avoidable errors.
                blocked = blocked_store.open_list(account)
                if blocked.peers and recipients is None:  # noqa: SIM102
                    before_block = len(recipient_items)
                    recipient_items = [(n, p) for (n, p) in recipient_items
                                       if not blocked.has(p)]
                    n_blocked = before_block - len(recipient_items)
                    if n_blocked:
                        skipped += n_blocked
                        print(f"[send] skipping {n_blocked} peer(s) Eitaa refuses "
                              f"from this account", flush=True)
                        await report(cards.card(
                            "⛔ SKIPPING REFUSED PEERS",
                            [("Phone    ", phone),
                             ("Skipped  ", f"{n_blocked:,} previously refused"),
                             ("Sending  ", f"{len(recipient_items):,}")],
                            footer="Eitaa refused these recipients from this account "
                                   "before (PEER_FLOOD and friends), and that does not "
                                   "expire on a timer. Use 'Reset Refused' on the "
                                   "account to try them again."))

                total = len(recipient_items)
                if total == 0 and skipped:
                    await report(cards.card(
                        "✅ NOTHING LEFT TO SEND",
                        [("Phone   ", phone),
                         ("Already ", f"{skipped:,} contact(s) received this content")],
                        footer="Every saved contact already got this exact message. "
                               "Set new content to send again."))
                    if agg is not None:
                        await agg.update(account, sent=0, failed=0, total=0,
                                         state="done", force=True)
                    return
                if total == 0:
                    # Bail out BEFORE the file upload. Two live runs uploaded a
                    # 9.5 MB file and then delivered it to nobody; one of them
                    # burned 10 minutes locating that upload first.
                    await report(cards.error_card(
                        "send", account, code="no_recipients",
                        detail="this account has no contacts to send to, so nothing "
                               "was uploaded or sent",
                        phase="targets", trace_id=job.job_id))
                    if agg is not None:
                        await agg.update(account, sent=0, failed=0, total=0,
                                         state="no_targets", force=True)
                    return
                # What is about to happen, and how long it should take. The
                # estimate reuses the last run's MEASURED per-message time,
                # because a run that would take hours used to look exactly like
                # one that would take three minutes.
                last_per = None
                try:
                    from bot.store import store as _store
                    last_per = ((_store.last_run or {}).get("timing") or {}).get("per_send")
                except Exception:  # noqa: BLE001
                    last_per = None
                file_mb = None
                if content.get("kind") == "file":
                    try:
                        file_mb = os.path.getsize(content.get("file_path", "")) / (1024 * 1024)
                    except OSError:
                        file_mb = None
                await report(cards.preflight_card(
                    phone, engine, kind, total, skipped, len(blocked.peers),
                    conc, delay, last_per, file_mb))

                # Opportunistically remember every peer we resolve here, so the
                # browser-free engine can reach these same contacts later with
                # no browser at all.
                await self._harvest_peers(driver, account, report,
                                          [p for _, p in recipient_items if p])

                # Build the transport for the chosen engine. Everything below is
                # engine-agnostic from here on.
                transport, engine = await self._build_transport(
                    engine, driver, account, report, stages)

                # Pre-warm the right bridge ONCE.
                if stages is not None:
                    await stages.begin(
                        "upload",
                        f"{total:,} recipient(s) ready · engine {engine}"
                        if total else None)
                file_bridge_ready = False
                if is_file:
                    # Upload the file a single time; every recipient then reuses
                    # that same uploaded document (no per-recipient re-upload ->
                    # no server strain).
                    finit = await transport.prepare_file(
                        content.get("file_path", ""), content.get("caption", ""))
                    if finit.get("ok"):
                        file_bridge_ready = True
                        print(f"[send] file uploaded ONCE (msg_id={finit.get('msg_id')}); "
                              f"reusing via sendMedia per recipient", flush=True)
                    else:
                        print(f"[send] file bridge init failed ({finit.get('code')})",
                              flush=True)
                        await report(cards.error_card(
                            "file_init", account, code="bridge_file_init",
                            detail=str(finit.get("code")), trace_id=job.job_id))
                        # A failed upload is usually still running inside the
                        # page: measured live, the renderer stayed so busy that
                        # even clicking the search box timed out after 30s. So
                        # the UI fallback cannot work either -- it ground through
                        # recipients for an hour and delivered nothing. Give the
                        # upload ONE more attempt on a fresh page, then stop.
                        if total > _MAX_UI_FILE_FALLBACKS:
                            if stages is not None:
                                await stages.begin("upload", "first upload failed - "
                                                             "reloading the page and "
                                                             "retrying once")
                            try:
                                if session.page is not None:
                                    # A browser-free run has no page to reload;
                                    # the retry below is still worth one attempt.
                                    await session.page.reload(
                                        wait_until="domcontentloaded")
                                    await session.page.wait_for_timeout(4000)
                                    await driver.ensure_bridge()
                            except Exception:  # noqa: BLE001
                                pass
                            finit = await transport.prepare_file(
                                content.get("file_path", ""), content.get("caption", ""))
                            if finit.get("ok"):
                                file_bridge_ready = True
                                print(f"[send] file uploaded on retry "
                                      f"(msg_id={finit.get('msg_id')})", flush=True)
                            else:
                                if stages is not None:
                                    await stages.fail("the file could not be uploaded")
                                    stages.stop()
                                    stages = None
                                await report(cards.card(
                                    "🛑 SEND STOPPED — UPLOAD FAILED",
                                    [("Phone", phone),
                                     ("Recipients", f"{total:,} waiting"),
                                     ("Reason", str(finit.get("code"))[:120])],
                                    footer="The shared upload failed twice, and the "
                                           "per-recipient browser upload would take "
                                           "~25s each (and usually fails while the "
                                           "page is still busy with the dead upload). "
                                           "Nothing was sent. Try a smaller file, or "
                                           "retry when the server is less loaded - "
                                           "press Send again and it resumes."))
                                return
                else:
                    # The page bridge is warmed even in hybrid mode: it is the
                    # safety net for any recipient the direct engine misses.
                    await driver.ensure_bridge()

                if stages is not None:
                    # The checklist hands over to the send progress card here.
                    await stages.done()
                    stages.stop()
                    stages = None
                if live is not None or agg is not None:
                    await self._send_progress(
                        live, agg, account, phone, sent=0, failed=0, total=total,
                        elapsed=0, status="🟢 Sending", state="running",
                        engine=engine, kind=kind, force=True)

                consecutive_failures = 0
                error_cards = 0
                via_bridge = 0      # sent through Eitaa's own engine (fast path)
                via_fallback = 0    # sent through the proven UI path
                reinits = 0         # times the in-page upload state was rebuilt
                ui_file_sends = 0   # file sends that had to re-upload per recipient
                limit_hits = 0      # server restrictions seen (PEER_FLOOD etc.)
                limit_cards = 0     # restriction cards posted (capped, not spam)
                waited_for_server = 0.0  # seconds spent honouring FLOOD_WAIT
                where = "send_file" if is_file else "send_text"
                text_body = content.get("text", "")
                caption_body = content.get("caption", "")

                # Timing instrumentation. 39 messages in 4 minutes (6.2s each) was
                # measured while the transport itself answered in 1-2s, so the
                # loop needs to be able to say WHERE the rest of the time went
                # instead of leaving it to guesswork.
                t_transport = 0.0
                t_fallback = 0.0
                n_transport = 0
                t_wait = 0.0

                async def _fast(peer: str | None):
                    """One fast-path (in-page API) send. No shared state, so a
                    batch of these can safely run concurrently."""
                    nonlocal t_transport, n_transport
                    if not peer:
                        return None
                    if is_file and not file_bridge_ready:
                        return None
                    t0 = time.time()
                    try:
                        if is_file:
                            return await transport.send_file(peer, caption_body)
                        return await transport.send_text(peer, text_body)
                    finally:
                        t_transport += time.time() - t0
                        n_transport += 1

                # `conc` recipients are attempted at once on the fast path. The
                # UI fallback below stays strictly sequential because it drives a
                # single browser page. conc=1 reproduces the old behaviour byte
                # for byte, which is why it stays the default.
                i = 0
                pos = 0
                limited = False
                while pos < total and not job.stop and not limited:
                    batch = recipient_items[pos:pos + conc]
                    pos += len(batch)
                    # Always via gather, even for a single recipient: it is the
                    # only form that turns an exception into a value instead of
                    # killing the job (and with it the unflushed ledger).
                    attempts = await asyncio.gather(
                        *[_fast(p) for _, p in batch], return_exceptions=True)

                    for (name, peer_id), b in zip(batch, attempts):
                        # NOTE: do NOT skip the rest of the batch on stop/limit.
                        # Those sends already reached the server, so dropping
                        # their results here would under-count them and deliver
                        # the same message again on the next run.
                        if (job.stop or limited) and not (
                                isinstance(b, dict) and b.get("ok")):
                            continue
                        if job.stop or limited:
                            # A success that landed after the stop: bank it so it
                            # is never sent twice, then stop.
                            sent += 1
                            via_bridge += 1
                            ledger.mark(name, peer_id)
                            continue
                        i += 1
                        if isinstance(b, BaseException):
                            b = {"ok": False, "code": f"{type(b).__name__}: {b}"}
                        try:
                            res = None
                            used_bridge = False

                            # A server-declared wait is an instruction, not a
                            # failure: honour short ones and carry on instead of
                            # abandoning the rest of the list.
                            if b is not None and b.get("limit"):
                                wait_s = _flood_wait(b.get("code"), b.get("wait"))
                                if wait_s and wait_s <= config.MAX_FLOOD_WAIT:
                                    print(f"[send] server asked for {wait_s}s; waiting",
                                          flush=True)
                                    waited_for_server += wait_s
                                    if await job.wait(wait_s):
                                        break
                                    b = await _fast(peer_id)
                                    if isinstance(b, BaseException):
                                        b = {"ok": False, "code": type(b).__name__}

                            if is_file and b is not None and not b.get("ok") \
                                    and not b.get("limit") \
                                    and "not initialized" in str(b.get("code", "")) \
                                    and reinits < _MAX_FILE_REINITS:
                                # The uploaded document lives in the page, so a
                                # reload wipes it. Rebuild ONCE and retry instead
                                # of re-uploading per recipient through the UI,
                                # which measured ~25s each and failed anyway.
                                reinits += 1
                                print(f"[send] upload state lost; re-initialising "
                                      f"(attempt {reinits})", flush=True)
                                again = await transport.prepare_file(
                                    content.get("file_path", ""), caption_body)
                                if again.get("ok"):
                                    print(f"[send] re-uploaded ONCE "
                                          f"(msg_id={again.get('msg_id')})", flush=True)
                                    b = await _fast(peer_id)
                                    if isinstance(b, BaseException):
                                        b = {"ok": False, "code": type(b).__name__}
                                else:
                                    file_bridge_ready = False
                                    await report(cards.error_card(
                                        "file_reinit", account,
                                        code="bridge_file_reinit",
                                        detail=str(again.get("code")),
                                        trace_id=job.job_id))

                            if b is not None:
                                if b.get("limit"):
                                    # The server refused this recipient. Whether
                                    # that ends the run is the owner's choice:
                                    # stop_on_limit=True pauses (safe, because
                                    # the server usually refuses everyone),
                                    # False just reports and carries on.
                                    limit_hits += 1
                                    # Remember a relationship-level refusal so
                                    # later runs do not waste time on this peer.
                                    blocked.add(peer_id, b.get("code"))
                                    if stop_on_limit:
                                        await report(cards.restriction_card(
                                            account, f"server: {b.get('code')}", sent))
                                        limited = True
                                    else:
                                        if limit_cards < 3:
                                            await report(cards.restriction_card(
                                                account, f"server: {b.get('code')}",
                                                sent, paused=False))
                                            limit_cards += 1
                                        failed += 1
                                        consecutive_failures += 1
                                        res = None
                                        b = None
                                        continue
                                elif b.get("ok"):
                                    used_bridge = True
                                    res = SendResult(
                                        ok=True, to=name,
                                        detail=f"bridge/{b.get('method')} id={b.get('msg_id')}")
                                else:
                                    print(f"[send] bridge miss for {name[:18]!r} "
                                          f"({b.get('code')}); UI fallback", flush=True)

                            # Fallback: proven UI send (covers no-peer_id, a
                            # bridge miss, or an unavailable file bridge). Always
                            # sequential -- it types into one page -- and always
                            # time-boxed: a UI file send hung on the FIRST
                            # recipient once and froze the entire run for 13
                            # minutes with no output at all.
                            if not limited and res is None:
                                budget = _UI_FILE_TIMEOUT if is_file else _UI_TEXT_TIMEOUT
                                t_fb = time.time()
                                try:
                                    if is_file:
                                        res = await asyncio.wait_for(
                                            driver.send_file(
                                                content.get("file_path", ""),
                                                caption=caption_body, query=name),
                                            timeout=budget)
                                    else:
                                        res = await asyncio.wait_for(
                                            driver.send_text(name, text_body, verify=True),
                                            timeout=budget)
                                except asyncio.TimeoutError:
                                    res = SendResult(
                                        ok=False, to=name,
                                        detail=f"ui_timeout after {int(budget)}s "
                                               f"(the slow browser path did not finish)")
                                    print(f"[send] UI path timed out for {name[:18]!r} "
                                          f"after {int(budget)}s; moving on", flush=True)
                                finally:
                                    t_fallback += time.time() - t_fb

                            if limited:
                                # Already surfaced above; not a per-send failure.
                                pass
                            elif res is not None and res.ok:
                                sent += 1
                                consecutive_failures = 0
                                ledger.mark(name, peer_id)
                                if used_bridge:
                                    via_bridge += 1
                                else:
                                    via_fallback += 1
                                    if is_file and peer_id:
                                        # A file that goes through the UI path is
                                        # re-uploaded for THIS recipient alone.
                                        ui_file_sends += 1
                            else:
                                failed += 1
                                consecutive_failures += 1
                                detail = res.detail if res is not None else "send produced no result"
                                # Surface EXACTLY why the send failed (capped to
                                # avoid spam).
                                if is_file and peer_id and not used_bridge:
                                    # A FAILED UI file attempt is just as
                                    # expensive as a successful one, so it has to
                                    # count towards the slow-path guard too -
                                    # otherwise a broken page grinds the whole
                                    # list at ~30-150s per recipient.
                                    ui_file_sends += 1
                                if error_cards < 12:
                                    await report(cards.error_card(
                                        where, account, target=name, code="send_failed",
                                        detail=detail, trace_id=job.job_id))
                                    error_cards += 1
                                if _is_limit(detail):
                                    limit_hits += 1
                                    if stop_on_limit:
                                        await report(cards.restriction_card(
                                            account, detail, sent))
                                        limited = True
                                    elif limit_cards < 3:
                                        await report(cards.restriction_card(
                                            account, detail, sent, paused=False))
                                        limit_cards += 1
                        except Exception as exc:  # noqa: BLE001
                            failed += 1
                            consecutive_failures += 1
                            if is_file and peer_id:
                                ui_file_sends += 1
                            if error_cards < 12:
                                await report(cards.error_card(
                                    where, account, target=name,
                                    code=type(exc).__name__, detail=str(exc),
                                    trace_id=job.job_id))
                                error_cards += 1

                        if limited:
                            # Do NOT break here: the remaining replies in this
                            # batch are already-completed sends, and the guard at
                            # the top of the loop banks them so nobody is sent to
                            # twice. The while-loop below is what stops the run.
                            continue

                        # Guard against the silent disaster: the fast upload is
                        # gone for good and every remaining recipient would be
                        # served by a full ~25-150s browser upload (attempted or
                        # failed - both cost the same). Stop and say why; the
                        # ledger means a re-run continues from here.
                        if is_file and ui_file_sends >= _MAX_UI_FILE_FALLBACKS:
                            await report(cards.card(
                                "🛑 SEND STOPPED — SLOW PATH",
                                [("Phone", phone),
                                 ("Sent", f"{sent:,} of {total:,}"),
                                 ("Failed", failed or None),
                                 ("Reason", f"the shared upload could not be reused for "
                                            f"{ui_file_sends} recipients")],
                                footer="Each of those falls back to uploading the whole "
                                       "file again through the browser (~25s+ each, and "
                                       "it usually fails), so the run was stopped instead "
                                       "of crawling through the rest. Press Send again to "
                                       "resume from here; nobody gets it twice."))
                            limited = True
                            break

                        # The brake used to trip at 5 consecutive failures, which
                        # killed a 1,099-contact run at recipient 300 over a
                        # recoverable page hiccup. A real restriction is caught
                        # by _is_limit above and stops instantly; this is only
                        # the "session is clearly broken" guard, so it needs a
                        # threshold a short rough patch cannot reach.
                        if consecutive_failures >= _FAILURE_BRAKE:
                            await report(cards.paused_card(
                                account,
                                f"{consecutive_failures} consecutive failures "
                                f"(sent {sent} of {total}; re-run to resume from here)",
                                sent))
                            limited = True
                            break

                        if live is not None or agg is not None:
                            await self._send_progress(
                                live, agg, account, phone, sent=sent, failed=failed,
                                total=total, elapsed=time.time() - start,
                                status="🟢 Sending", state="running",
                                engine=engine, kind=kind)
                        elif i % log_every == 0:
                            await report(cards.send_progress(
                                sent, failed, skipped, total - i, time.time() - start))

                    if limited or job.stop:
                        break
                    # One pace interval per batch: with conc>1 that is the whole
                    # point, `conc` messages per `delay` instead of one.
                    # Interruptible: a stop wakes this immediately.
                    t_w = time.time()
                    stop_now = await job.wait(delay)
                    t_wait += time.time() - t_w
                    if stop_now:
                        break

                # Persist whatever was delivered, even on a stop or a crash path,
                # so the next run resumes instead of re-sending.
                ledger.flush()
                hybrid_stats = None
                if isinstance(transport, transports.HybridTransport):
                    hybrid_stats = dict(transport.stats)
                    print(f"[engine] hybrid split: direct={hybrid_stats['direct']} "
                          f"bridge={hybrid_stats['bridge']} "
                          f"fell_back={hybrid_stats['fell_back']}", flush=True)
                # Evidence of which path carried the load (fast bridge vs the UI
                # fallback) so alternative methods stay observable at a glance.
                elapsed_total = max(0.001, time.time() - start)
                other = max(0.0, elapsed_total - t_transport - t_fallback - t_wait)
                timing = {
                    "total": round(elapsed_total, 1),
                    "transport": round(t_transport, 1),
                    "fallback": round(t_fallback, 1),
                    "pacing": round(t_wait, 1),
                    "other": round(other, 1),
                    "per_send": round(t_transport / n_transport, 2) if n_transport else None,
                    "msg_per_s": round((sent / elapsed_total), 2),
                }
                print(f"[send] timing: total={timing['total']}s "
                      f"transport={timing['transport']}s "
                      f"fallback={timing['fallback']}s pacing={timing['pacing']}s "
                      f"other={timing['other']}s per_send={timing['per_send']}s "
                      f"rate={timing['msg_per_s']}/s", flush=True)
                print(f"[send] path summary: via_bridge={via_bridge} "
                      f"via_fallback={via_fallback} reinits={reinits} sent={sent} "
                      f"failed={failed} skipped={skipped} conc={conc} "
                      f"limits={limit_hits} server_wait={int(waited_for_server)}s "
                      f"({'file' if is_file else 'text'})", flush=True)
                job.summary = {"sent": sent, "failed": failed, "skipped": skipped,
                               "total": total, "via_bridge": via_bridge,
                               "via_fallback": via_fallback, "reinits": reinits,
                               "concurrency": conc, "engine": engine,
                               "limits": limit_hits, "timing": timing,
                               "refused_added": blocked.added,
                               "browserless": browserless,
                               "hybrid": hybrid_stats}
                if agg is None:
                    await report(cards.timing_card(phone, engine, timing, conc,
                                                   limit_hits, via_fallback))
                # Remember the outcome so the home card shows a real last result
                # instead of nothing once the job is gone.
                try:
                    from bot.store import store as _store
                    _store.set_last_run(account=phone, kind=kind, sent=sent,
                                        failed=failed, skipped=skipped, total=total,
                                        elapsed=time.time() - start,
                                        engine=engine, timing=timing,
                                        stopped=bool(job.stop))
                except Exception:  # noqa: BLE001 - reporting must never break a job
                    pass
                stopped_early = bool(job.stop) or limited
                await self._send_progress(
                    live, agg, account, phone, sent=sent, failed=failed,
                    total=total, elapsed=time.time() - start,
                    status="🛑 Stopped" if stopped_early else "✅ Done",
                    state="stopped" if stopped_early else "done",
                    engine=engine, kind=kind, force=True)
                # In a multi-account run the combined summary is posted once by
                # the supervisor, so skip the per-account finish card.
                if agg is None:
                    # `limited` covers a server restriction, the failure brake
                    # and the dead-upload guard. Reporting those as a clean
                    # "FINISHED" would hide that the list was cut short.
                    await report(cards.send_finished(
                        account, kind, sent, failed, skipped, total,
                        time.time() - start, stopped=bool(job.stop) or limited))
        except Exception as exc:  # noqa: BLE001
            if stages is not None:
                await stages.fail(f"{type(exc).__name__}: {str(exc)[:120]}")
            if agg is not None:
                await agg.update(account, state="failed", force=True)
            await report(cards.error_card("send_job", account, code=type(exc).__name__,
                                          detail=str(exc), trace_id=job.job_id))
        finally:
            if stages is not None:
                # Also covers the Force Stop (cancellation) path, so the card
                # never sits there pretending to still work.
                stages.stop()
            # The live card paints in the background now, so the final state has
            # to be pushed out explicitly when the job ends.
            for card_obj in (live, getattr(agg, "live", None)):
                flush = getattr(card_obj, "flush", None)
                if flush is not None:
                    try:
                        await flush()
                    except Exception:  # noqa: BLE001
                        pass
            if transport is not None:
                try:
                    await transport.close()
                except Exception:  # noqa: BLE001
                    pass
            # Runs on the cancellation path too, so a Force Stop keeps its
            # delivered-to record and the re-run does not repeat those sends.
            if ledger is not None:
                try:
                    ledger.flush()
                except Exception:  # noqa: BLE001
                    pass
            # Refusals must survive a Force Stop too: without this, the peers
            # learned during a cancelled run were forgotten and retried forever.
            if blocked is not None and blocked.added:
                try:
                    blocked.flush()
                    print(f"[send] {blocked.added} peer(s) added to the refused "
                          f"list ({len(blocked.peers)} total)", flush=True)
                except Exception:  # noqa: BLE001
                    pass
            self._busy.discard(account)

    # ---- peer harvesting (what the browser-free sender needs) ----
    async def _harvest_peers(self, driver, account: str, report: Report,
                             peer_ids: list) -> int:
        """Ask Eitaa's own peer manager for the access_hash of known peers and
        persist them, so the browser-free engine can reach these contacts.

        Silent on failure: harvesting is a bonus, never a reason to fail a job.
        """
        if not peer_ids:
            return 0
        try:
            from direct import peers as peer_store
            res = await driver.bridge_harvest_peers(peer_ids)
            if not res.get("ok"):
                return 0
            found = res.get("peers") or []
            if not found:
                return 0
            new = peer_store.save_users(account, found)
            if new:
                await report(cards.peers_saved(account, new, peer_store.count(account),
                                               source="contacts list"))
            return new
        except Exception as exc:  # noqa: BLE001
            print(f"[peers] harvest skipped: {type(exc).__name__}: {exc}", flush=True)
            return 0

    async def _save_imported_peers(self, account: str, report: Report,
                                   added: list | None) -> int:
        """Persist the user_id + access_hash of freshly imported contacts.

        `contacts.importContacts` answers with the matched users AND their
        access_hash. That is exactly the pair the browser-free sender needs, so
        every import now feeds the peer store instead of only being counted.
        """
        rows = [a for a in (added or []) if a and a.get("access_hash")]
        if not rows:
            return 0
        try:
            from direct import peers as peer_store
            users = [{"user_id": a.get("user_id"),
                      "access_hash": a.get("access_hash"),
                      "label": a.get("phone")} for a in rows]
            new = peer_store.save_users(account, users)
            if new:
                await report(cards.peers_saved(account, new, peer_store.count(account),
                                               source="importContacts"))
            return new
        except Exception as exc:  # noqa: BLE001
            print(f"[peers] save skipped: {type(exc).__name__}: {exc}", flush=True)
            return 0

    # ---- send job (DIRECT / browser-free MTProto) ----
    async def _send_job_direct(self, job: Job, content: dict, settings: dict,
                               report: Report, recipients: list[str] | None,
                               live=None, account_phone: str | None = None,
                               agg: AggregateProgress | None = None) -> None:
        """Send text or a file to every saved peer with NO browser.

        Uses direct/sender.py (the reusable form of the proven `direct-send` /
        `direct-send-file` commands) and direct/peers.py for the targets. A file
        is uploaded ONCE and then re-sent to each recipient, exactly like the
        browser file bridge does.

        Targets come from the peer store, which is filled while contacts are
        built or collected. With no saved peers this job cannot run, and says so
        instead of silently sending nothing.
        """
        account = job.account
        phone = account_phone or account
        engine = "direct"
        is_file = content.get("kind") == "file"
        kind = "File" if is_file else "Text"
        delay = float(settings.get("text_send_delay", config.TEXT_SEND_DELAY))
        log_every = int(settings.get("send_log_every", config.SEND_LOG_EVERY))
        text_body = content.get("text", "")
        caption_body = content.get("caption", "")
        where = "direct_send_file" if is_file else "direct_send"

        self._busy.add(account)
        start = time.time()
        sent = failed = skipped = 0
        sender = None
        try:
            from direct import peers as peer_store
            from direct.sender import DirectSender, SenderError

            targets = peer_store.targets(account)
            if recipients:
                wanted = {str(r) for r in recipients}
                targets = [(label, p) for label, p in targets if label in wanted]
                # Names with no saved peer can't be reached browser-free.
                skipped = max(0, len(wanted) - len(targets))
            if not targets:
                await report(cards.error_card(
                    where, account, code="no_peers", engine=engine, phase="targets",
                    trace_id=job.job_id,
                    detail="no saved peers for this account, so the browser-free "
                           "engine has nobody to send to. Open this account and tap "
                           "'🔄 Update Contacts' (reads your contacts via the "
                           "browser), then send again. Or switch the engine to "
                           "bridge."))
                if agg is not None:
                    await agg.update(account, state="no_targets", force=True)
                return

            try:
                sender = await asyncio.to_thread(DirectSender, account)
            except SenderError as exc:
                await report(cards.error_card(
                    where, account, code="no_session", engine=engine,
                    phase="load_context", detail=str(exc), trace_id=job.job_id))
                if agg is not None:
                    await agg.update(account, state="failed", force=True)
                return

            total = len(targets)
            if live is not None or agg is not None:
                await self._send_progress(
                    live, agg, account, phone, sent=0, failed=0, total=total,
                    elapsed=0, status="🟢 Sending", state="running",
                    engine=engine, kind=kind, force=True)
            else:
                await report(cards.send_started(account, kind, total, delay))

            # Upload the file a single time; every recipient reuses it.
            if is_file:
                up = await asyncio.to_thread(sender.upload_file,
                                             content.get("file_path", ""))
                if not up.get("ok"):
                    await report(cards.error_card(
                        where, account, code="upload_failed", engine=engine,
                        phase="upload", detail=str(up.get("code")),
                        trace_id=job.job_id))
                    if agg is not None:
                        await agg.update(account, state="failed", force=True)
                    return
                print(f"[dsend] uploaded ONCE: {up.get('name')} "
                      f"{up.get('size')}B in {up.get('parts')} part(s) "
                      f"-> {up.get('host')}", flush=True)

            consecutive_failures = 0
            error_cards = 0
            for i, (label, peer) in enumerate(targets, start=1):
                if job.stop:
                    break
                limited = False
                try:
                    if is_file:
                        res = await asyncio.to_thread(
                            sender.send_uploaded_file, peer, caption_body)
                    else:
                        res = await asyncio.to_thread(
                            sender.send_text, peer, text_body)
                except Exception as exc:  # noqa: BLE001
                    res = {"ok": False, "code": f"{type(exc).__name__}: {exc}"}

                if res.get("ok"):
                    sent += 1
                    consecutive_failures = 0
                else:
                    detail = str(res.get("code") or "send produced no result")
                    if res.get("limit") or _is_limit(detail):
                        await report(cards.restriction_card(
                            account, f"server: {detail}", sent))
                        limited = True
                    else:
                        failed += 1
                        consecutive_failures += 1
                        if error_cards < 12:
                            await report(cards.error_card(
                                where, account, target=label, code="send_failed",
                                detail=detail, engine=engine, trace_id=job.job_id))
                            error_cards += 1
                if limited:
                    if agg is not None:
                        await agg.update(account, sent=sent, failed=failed,
                                         total=total, state="limited", force=True)
                    break

                if consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES:
                    await report(cards.paused_card(
                        account, f"{consecutive_failures} consecutive failures", sent))
                    break

                if live is not None or agg is not None:
                    await self._send_progress(
                        live, agg, account, phone, sent=sent, failed=failed,
                        total=total, elapsed=time.time() - start,
                        status="🟢 Sending", state="running",
                        engine=engine, kind=kind)
                elif i % log_every == 0:
                    await report(cards.send_progress(
                        sent, failed, skipped, total - i, time.time() - start))

                if await job.wait(delay):
                    break

            print(f"[dsend] path=direct sent={sent} failed={failed} of {total} "
                  f"({'file' if is_file else 'text'})", flush=True)
            job.summary = {"sent": sent, "failed": failed, "skipped": skipped,
                           "total": total, "via_direct": sent, "via_fallback": 0}
            await self._send_progress(
                live, agg, account, phone, sent=sent, failed=failed, total=total,
                elapsed=time.time() - start,
                status="🛑 Stopped" if job.stop else "✅ Done",
                state="stopped" if job.stop else "done",
                engine=engine, kind=kind, force=True)
            if agg is None:
                await report(cards.send_finished(
                    account, kind, sent, failed, skipped, total,
                    time.time() - start, stopped=job.stop))
        except Exception as exc:  # noqa: BLE001
            if agg is not None:
                await agg.update(account, state="failed", force=True)
            await report(cards.error_card(where, account, code=type(exc).__name__,
                                          detail=str(exc), engine=engine,
                                          phase="loop", trace_id=job.job_id))
        finally:
            if sender is not None:
                try:
                    await asyncio.to_thread(sender.close)
                except Exception:  # noqa: BLE001
                    pass
            self._busy.discard(account)

    # ---- contacts job (bridge / browser) ----
    async def _contacts_job(self, job: Job, prefix: str, count: int,
                            settings: dict, report: Report, live=None,
                            account_phone: str | None = None) -> None:
        account = job.account
        phone = account_phone or account
        engine = effective_engine(settings)
        delay = float(settings.get("contact_create_delay", config.CONTACT_CREATE_DELAY))
        log_every = int(settings.get("send_log_every", config.SEND_LOG_EVERY))
        entries, err = expand_range(prefix, count)
        if err:
            await report(cards.error_card("contacts_range", account, code="bad_range",
                                          detail=err, engine=engine, phase="expand_range"))
            return
        # The imported contact's display name is the ACCOUNT's OWN phone (per spec).
        for e in entries:
            e["first"] = phone
        self._busy.add(account)
        start = time.time()
        added = not_on = invalid = error = 0
        total = len(entries)
        try:
            async with session_pool.lease(account, headed=config.HEADED_JOBS) as session:
                driver = EitaaDriver(session)
                await driver.open()
                if not await driver.is_logged_in():
                    await report(cards.error_card("contacts", account, code="not_logged_in",
                                                  detail="account is not logged in"))
                    return
                if live is not None:
                    await live.set(cards.live_contacts(phone, prefix, 0, 0, total,
                                                       status="🟢 Searching", engine=engine))
                else:
                    await report(cards.contacts_started(account, prefix, total, delay))

                BATCH = 50
                done = 0
                aborted = False
                plus_prefix = False
                use_bridge = await driver.ensure_contacts_bridge()

                if use_bridge:
                    # PROBE FIRST. If the phone format is not the one this Eitaa
                    # build expects, the server matches nobody and answers
                    # "imported: 0" with NO error -- which looked exactly like the
                    # job racing through and building nothing. So try both
                    # formats on the first batch and report the raw counts.
                    probe = entries[:min(BATCH, total)]
                    tried: list[dict] = []
                    chosen: str | None = None
                    for plus in (False, True):
                        r = await driver.bridge_import_contacts(probe, plus_prefix=plus)
                        fmt = "+98" if plus else "98"
                        if r.get("limit"):
                            wait = r.get("wait")
                            detail = (f"server: {r.get('code')}"
                                      + (f" (wait {wait}s)" if wait else ""))
                            await report(cards.restriction_card(account, detail, added))
                            aborted = True
                            use_bridge = False
                            break
                        if not r.get("ok"):
                            tried.append({"format": fmt, "batch": len(probe),
                                          "code": r.get("code")})
                            continue
                        imp = int(r.get("imported_count", 0))
                        tried.append({"format": fmt, "batch": len(probe),
                                      "imported": imp,
                                      "users": int(r.get("users_count", 0)),
                                      "retry": int(r.get("retry_count", 0))})
                        if imp > 0:
                            # This format works -> keep its result and use it for
                            # the remaining batches.
                            chosen = fmt
                            plus_prefix = plus
                            rc = int(r.get("retry_count", 0))
                            added += imp
                            not_on += max(0, len(probe) - imp - rc)
                            error += rc
                            await self._save_imported_peers(account, report, r.get("added"))
                            done = len(probe)
                            break
                    if not aborted:
                        await report(cards.contacts_probe(
                            account, tried, chosen, fallback=chosen is None))
                        if chosen is None:
                            # Neither format matched anyone. Rather than report a
                            # silent zero, fall through to the proven per-number
                            # UI add flow, which reports a real reason per number.
                            use_bridge = False

                if use_bridge:
                    # FAST PATH: contacts.importContacts in batches. The server
                    # returns exactly which numbers are on Eitaa (as users WITH
                    # access_hash -> instantly sendable). No per-number UI popup,
                    # no server strain.
                    while done < total:
                        if job.stop:
                            break
                        batch = entries[done:done + BATCH]
                        r = await driver.bridge_import_contacts(batch, plus_prefix=plus_prefix)
                        if r.get("limit"):
                            wait = r.get("wait")
                            detail = f"server: {r.get('code')}" + (f" (wait {wait}s)" if wait else "")
                            await report(cards.restriction_card(account, detail, added))
                            break
                        if r.get("ok"):
                            imp = int(r.get("imported_count", 0))
                            rc = int(r.get("retry_count", 0))
                            added += imp
                            not_on += max(0, len(batch) - imp - rc)
                            error += rc
                            # Keep the access_hash of every matched user: this is
                            # what the browser-free sender needs to reach them.
                            await self._save_imported_peers(account, report, r.get("added"))
                        else:
                            error += len(batch)
                            await report(cards.error_card(
                                "import_contacts", account, code="import_failed",
                                detail=str(r.get("code")), trace_id=job.job_id))
                        done += len(batch)
                        if live is not None:
                            await live.set(cards.live_contacts(
                                phone, prefix, added, done, total,
                                status="🟢 Searching", engine=engine,
                                not_on=not_on, failed=error))
                        else:
                            await report(cards.contacts_progress(
                                added, not_on, invalid, error, total - done))
                        if await job.wait(delay):
                            break
                    print(f"[contacts] path=bridge format={'+98' if plus_prefix else '98'} "
                          f"added={added} not_on={not_on} error={error} of {total}",
                          flush=True)
                elif not aborted:
                    # FALLBACK: the proven (slower) per-number UI add flow. Used
                    # when the bridge is unavailable OR when it matched nobody.
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

                        if live is not None:
                            await live.set(cards.live_contacts(
                                phone, prefix, added, i, total,
                                status="🟢 Searching", engine=engine,
                                not_on=not_on, failed=error))
                        elif i % log_every == 0:
                            await report(cards.contacts_progress(
                                added, not_on, invalid, error, total - i))
                        if await job.wait(delay):
                            break

                    await driver._reset_to_contacts_view()
                    print(f"[contacts] path=UI added={added} not_on={not_on} "
                          f"error={error} of {total}", flush=True)

                job.summary = {"added": added, "not_on": not_on, "invalid": invalid,
                               "error": error, "total": total}
                if live is not None:
                    await live.set(cards.live_contacts(
                        phone, prefix, added, total if not job.stop else added + not_on + invalid + error,
                        total, status="🛑 Stopped" if job.stop else "✅ Done",
                        engine=engine, not_on=not_on, failed=error), force=True)
                await report(cards.contacts_finished(
                    account, added, not_on, invalid, error, total,
                    time.time() - start, stopped=job.stop))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("contacts_job", account, code=type(exc).__name__,
                                          detail=str(exc), trace_id=job.job_id, engine=engine,
                                          phase="bridge_contacts"))
        finally:
            self._busy.discard(account)

    # ---- contacts job (DIRECT / browser-free MTProto) ----
    async def _contacts_job_direct(self, job: Job, prefix: str, count: int,
                                   settings: dict, report: Report, live=None,
                                   account_phone: str | None = None) -> None:
        """Build contacts with NO browser: contacts.importContacts in batches
        straight over HTTPS, using the account's captured session context."""
        account = job.account
        phone = account_phone or account
        delay = float(settings.get("contact_create_delay", config.CONTACT_CREATE_DELAY))
        entries, err = expand_range(prefix, count)
        if err:
            await report(cards.error_card("contacts_range", account, code="bad_range",
                                          detail=err, engine="direct", phase="expand_range"))
            return
        for e in entries:
            e["first"] = phone

        # Load the browser-free session context from this account's newest
        # capture. DirectSender owns that logic (and picks the API host the
        # browser itself used, instead of a hardcoded one).
        try:
            from direct.sender import DirectSender, SenderError
            try:
                sender = await asyncio.to_thread(DirectSender, account)
            except SenderError as exc:
                await report(cards.error_card(
                    "contacts_direct", account, code="no_session", engine="direct",
                    phase="load_context", detail=str(exc)))
                return
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("contacts_direct", account, code=type(exc).__name__,
                                          detail=str(exc), engine="direct", phase="load_context"))
            return

        self._busy.add(account)
        start = time.time()
        added = not_on = error = 0
        total = len(entries)
        BATCH = 100
        try:
            if live is not None:
                await live.set(cards.live_contacts(phone, prefix, 0, 0, total,
                                                   status="🟢 Searching", engine="direct"))
            done = 0
            plus_prefix = True   # expand_range produces "+98..."

            # PROBE FIRST, same as the bridge path: a wrong phone format makes the
            # server match NOBODY and answer with no error, which is exactly the
            # "raced through and built nothing" symptom. Try both formats on the
            # first batch and post the raw counts.
            probe = entries[:min(BATCH, total)]
            tried: list[dict] = []
            chosen: str | None = None
            for plus in (True, False):
                r = await asyncio.to_thread(sender.import_contacts, probe, plus)
                fmt = "+98" if plus else "98"
                if r.get("limit"):
                    await report(cards.restriction_card(
                        account, f"server: {r.get('code')}", added))
                    chosen = None
                    tried.append({"format": fmt, "batch": len(probe),
                                  "code": r.get("code")})
                    break
                if not r.get("ok"):
                    tried.append({"format": fmt, "batch": len(probe),
                                  "code": r.get("code")})
                    continue
                imp = int(r.get("imported", 0))
                tried.append({"format": fmt, "batch": len(probe), "imported": imp,
                              "users": len(r.get("imported_ids") or []),
                              "retry": 0, "parse_ok": r.get("parse_ok"),
                              "cid": r.get("cid"), "head": r.get("head")})
                if imp > 0:
                    chosen = fmt
                    plus_prefix = plus
                    added += imp
                    not_on += max(0, len(probe) - imp)
                    done = len(probe)
                    break
            note = None
            if chosen is None:
                note = ("Neither phone format worked, so this job is handing over to "
                        "the bridge (browser) path, which is proven. Nothing is lost.")
            await report(cards.contacts_probe(account, tried, chosen, note=note))

            if chosen is None:
                # LIVE FINDING (2026-07-25): Eitaa answers importContacts on the
                # browser-free path with a 4-byte, payload-less reply
                # cid=0xdc252379 -- not contacts.importedContacts, and not any
                # standard Telegram constructor. Both phone formats got it, so the
                # format was never the problem: this endpoint simply does not serve
                # contact import off the browser path.
                # Rather than leaving the owner with a dead engine, hand the whole
                # job over to the bridge path, which is proven AND harvests peers.
                try:
                    await asyncio.to_thread(sender.close)
                except Exception:  # noqa: BLE001
                    pass
                self._busy.discard(account)
                print("[contacts] direct import unsupported "
                      "(reply cid=0xdc252379) -> handing over to bridge", flush=True)
                await report(cards.card(
                    "🔄 SWITCHING TO BRIDGE",
                    [("Account", phone), ("Reason ", "direct import not served")],
                    footer="Building these contacts through the browser instead. This "
                           "also harvests peers, so fast sending keeps working."))
                bridge_settings = dict(settings)
                bridge_settings["engine"] = "bridge"
                await self._contacts_job(job, prefix, count, bridge_settings,
                                         report, live, phone)
                return

            while done < total:
                if job.stop:
                    break
                batch = entries[done:done + BATCH]
                # Blocking HTTPS in a worker thread so the panel stays responsive.
                r = await asyncio.to_thread(sender.import_contacts, batch, plus_prefix)
                if r.get("limit"):
                    await report(cards.restriction_card(
                        account, f"server: {r.get('code')}", added))
                    break
                if not r.get("ok"):
                    error += len(batch)
                    await report(cards.error_card(
                        "import_contacts", account, code="import_failed",
                        detail=str(r.get("code")), engine="direct",
                        phase="importContacts", trace_id=job.job_id))
                elif not r.get("parse_ok", True):
                    # The call succeeded but the reply was not importedContacts,
                    # so "imported 0" would be a lie. Report it as an error.
                    error += len(batch)
                    cid = r.get("cid")
                    await report(cards.error_card(
                        "import_contacts", account, code="unexpected_reply",
                        detail=f"reply cid={('0x%08x' % cid) if cid else '?'} "
                               f"head={r.get('head')}",
                        engine="direct", phase="parse_reply", trace_id=job.job_id))
                    break
                else:
                    imp = int(r.get("imported", 0))
                    added += imp
                    not_on += max(0, len(batch) - imp)
                done += len(batch)
                if live is not None:
                    await live.set(cards.live_contacts(
                        phone, prefix, added, done, total, status="🟢 Searching",
                        engine="direct", not_on=not_on, failed=error))
                if await job.wait(delay):
                    break
            print(f"[contacts] path=direct added={added} not_on={not_on} "
                  f"error={error} of {total}", flush=True)
            job.summary = {"added": added, "not_on": not_on, "invalid": 0,
                           "error": error, "total": total}
            if live is not None:
                await live.set(cards.live_contacts(
                    phone, prefix, added, done, total,
                    status="🛑 Stopped" if job.stop else "✅ Done",
                    engine="direct", not_on=not_on, failed=error), force=True)
            await report(cards.contacts_finished(
                account, added, not_on, 0, error, total, time.time() - start, stopped=job.stop))
            if added:
                # The direct path can count matches but cannot safely read each
                # user's access_hash (Eitaa's User row constructor is unknown and
                # guessing it would create silently-wrong peers). Say so, so the
                # owner knows how to make these contacts fast-sendable.
                await report(cards.card(
                    "ℹ️ PEERS NOT HARVESTED",
                    [("Account", phone), ("Imported", added), ("Engine ", "direct")],
                    footer="Contacts were created, but the direct engine can't read their "
                           "access_hash. Run Build Contacts or a Send once with the bridge "
                           "engine to harvest peers, then fast send can reach them."))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("contacts_direct_job", account, code=type(exc).__name__,
                                          detail=str(exc), engine="direct", phase="loop",
                                          trace_id=job.job_id))
        finally:
            try:
                await asyncio.to_thread(sender.close)
            except Exception:  # noqa: BLE001
                pass
            self._busy.discard(account)


manager = JobManager()
