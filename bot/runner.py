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
from eitaa.driver import EitaaDriver, SendResult
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


@dataclass
class LoginState:
    """Tracks an in-progress bridge login waiting for the user's code."""
    account: str
    phone: str
    code_future: asyncio.Future
    stage: str = "sending"   # sending | awaiting_code | done


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
        states = {r["state"] for r in self.breakdown()}
        if states & {"running"}:
            return "🟢 Sending"
        if states & {"limited"}:
            return "🚫 Limited"
        if states & {"stopped"}:
            return "🛑 Stopped"
        if states and states <= {"done", "failed", "stopped", "limited"}:
            return "✅ Done"
        return "⏳ Starting"

    def render(self) -> str:
        sent, failed, total = self.totals()
        return cards.live_send_multi(
            self.breakdown(), self.current, sent, failed, total,
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
                       report: Report, recipients: list[str] | None = None,
                       live=None, account_phone: str | None = None,
                       agg: AggregateProgress | None = None) -> Job:
        """Start a send job, honouring the engine setting.

        engine=direct -> browser-free MTProto (no Chromium at all), which needs
        saved peers for the targets. engine=bridge -> the proven tweb path.
        Routing mirrors run_contacts, which already dispatched on the engine.
        """
        job = self._new_job("send", account)
        engine = str(settings.get("engine", config.ENGINE))
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
                             settings: dict, report: Report, live=None) -> list[Job]:
        """Send from SEVERAL accounts at the same time, into ONE live card.

        `accounts` is [(account, phone)]. Accounts already running a job are
        skipped (one job per account stays enforced). Every job reports into a
        shared AggregateProgress, and a supervisor posts the final summary once
        they have all finished.
        """
        free = [(a, p) for a, p in accounts if not self.is_busy(a)]
        if not free:
            return []
        kind = "File" if content.get("kind") == "file" else "Text"
        engine = str(settings.get("engine", config.ENGINE))
        agg = AggregateProgress(live, free, kind=kind, engine=engine)
        if live is not None:
            await live.set(agg.render(), force=True)
        jobs: list[Job] = []
        for acc, phone in free:
            jobs.append(await self.run_send(
                acc, content, settings, report, live=None,
                account_phone=phone, agg=agg))
        asyncio.create_task(self._multi_send_supervisor(agg, jobs, report, kind, engine))
        return jobs

    async def _multi_send_supervisor(self, agg: AggregateProgress, jobs: list[Job],
                                     report: Report, kind: str, engine: str) -> None:
        """Wait for every account's job, then post one combined summary."""
        tasks = [j.task for j in jobs if j.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await agg.finish()
        sent, failed, total = agg.totals()
        stopped = any(j.stop for j in jobs)
        await report(cards.multi_send_finished(
            agg.breakdown(), sent, failed, total, time.time() - agg.start,
            kind=kind, engine=engine, stopped=stopped))

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

    async def run_contacts(self, account: str, prefix: str, count: int,
                           settings: dict, report: Report, live=None,
                           account_phone: str | None = None) -> Job:
        job = self._new_job("contacts", account)
        engine = str(settings.get("engine", config.ENGINE))
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
    async def start_bridge_login(self, account: str, phone: str, report: Report) -> bool:
        """Begin a no-noVNC login: send the code, then wait for the user's code.

        Returns False if the account is busy. The code is delivered later via
        submit_login_code().
        """
        if account in self._busy:
            return False
        self._busy.add(account)
        asyncio.create_task(self._bridge_login_job(account, phone, report))
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

    async def _bridge_login_job(self, account: str, phone: str, report: Report) -> None:
        from capture.browser import open_session
        from eitaa.driver import EitaaDriver
        from eitaa.login_flow import (
            normalize_phone_intl, resolve_api_creds, send_code, sign_in,
        )

        state = LoginState(account, phone, asyncio.get_event_loop().create_future())
        self._logins[account] = state
        api_id, api_hash = resolve_api_creds()
        intl = normalize_phone_intl(phone)
        try:
            async with open_session(account) as session:
                await session.goto()
                driver = EitaaDriver(session)
                await driver.open()

                if await driver.is_logged_in():
                    await report(cards.card(
                        "👤 ACCOUNT READY",
                        [("Account", account), ("Status", "already logged in")]))
                    return

                sc = await send_code(driver, intl, api_id, api_hash)
                if not sc.get("ok"):
                    code = str(sc.get("code", ""))
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

                # finalize (setUserAuth) already ran in-page; confirm live, else reload.
                await session.page.wait_for_timeout(1500)
                logged = await driver.is_logged_in()
                if not logged:
                    try:
                        await session.page.reload(wait_until="domcontentloaded")
                    except Exception:  # noqa: BLE001
                        pass
                    await session.page.wait_for_timeout(6000)
                    logged = await driver.is_logged_in()

                if logged:
                    # On-add stats: fetch contacts + chats and persist account meta.
                    contacts = pvs = None
                    try:
                        s = await driver.bridge_stats()
                        if s:
                            contacts, pvs = s.get("contacts"), s.get("pvs")
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
                    await report(cards.account_added(account, phone_digits, contacts, pvs, engine))
                else:
                    await report(cards.card(
                        "⚠️ LOGIN INCOMPLETE",
                        [("Account", account)],
                        footer="signIn succeeded but the app didn't switch to logged-in. "
                               "Try again, or use the noVNC button."))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card(
                "bridge_login", account, code=type(exc).__name__, detail=str(exc)))
        finally:
            self._logins.pop(account, None)
            self._busy.discard(account)

    # ---- send job ----
    async def _send_job(self, job: Job, content: dict, settings: dict,
                        report: Report, recipients: list[str] | None,
                        live=None, account_phone: str | None = None,
                        agg: AggregateProgress | None = None) -> None:
        account = job.account
        phone = account_phone or account
        # This is the BRIDGE job: it drives Eitaa Web (tweb) in Chromium and
        # uses Eitaa's own send engine via peer_id where possible. The
        # browser-free path lives in _send_job_direct.
        engine = "bridge"
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

                is_file = content.get("kind") == "file"
                if recipients is None:
                    contacts = await driver.collect_all_contacts()
                    # Keep BOTH title and peer_id: peer_id drives the fast bridge
                    # send (Eitaa's own engine, no UI), title is the search
                    # fallback + failure label. Collecting also warms tweb's peer
                    # cache so peer resolution works for the bridge.
                    recipient_items = [
                        (c.get("title", ""), c.get("peer_id"))
                        for c in contacts if c.get("title")
                    ]
                    # collect_all_contacts leaves the Contacts subview open;
                    # return to the main chat list before we start opening chats.
                    await driver._return_to_chat_list()
                else:
                    # Externally-supplied recipients are plain names -> no peer_id,
                    # so they use the proven UI flow unchanged.
                    recipient_items = [(name, None) for name in recipients]
                total = len(recipient_items)
                if live is not None or agg is not None:
                    await self._send_progress(
                        live, agg, account, phone, sent=0, failed=0, total=total,
                        elapsed=0, status="🟢 Sending", state="running",
                        engine=engine, kind=kind, force=True)
                else:
                    await report(cards.send_started(account, kind, total, delay))

                # Opportunistically remember every peer we resolve here, so the
                # browser-free engine can reach these same contacts later with
                # no browser at all.
                await self._harvest_peers(driver, account, report,
                                          [p for _, p in recipient_items if p])

                # Pre-warm the right bridge ONCE.
                file_bridge_ready = False
                if is_file:
                    # Upload the file a single time; every recipient then reuses
                    # that same uploaded document (no per-recipient re-upload ->
                    # no server strain).
                    finit = await driver.bridge_file_init(
                        content.get("file_path", ""), content.get("caption", ""))
                    if finit.get("ok"):
                        file_bridge_ready = True
                        print(f"[send] file uploaded ONCE (msg_id={finit.get('msg_id')}); "
                              f"reusing via sendMedia per recipient", flush=True)
                    else:
                        print(f"[send] file bridge init failed ({finit.get('code')}); "
                              f"falling back to UI file send", flush=True)
                        await report(cards.error_card(
                            "file_init", account, code="bridge_file_init",
                            detail=str(finit.get("code")), trace_id=job.job_id))
                else:
                    await driver.ensure_bridge()

                consecutive_failures = 0
                error_cards = 0
                via_bridge = 0      # sent through Eitaa's own engine (fast path)
                via_fallback = 0    # sent through the proven UI path
                where = "send_file" if is_file else "send_text"
                text_body = content.get("text", "")
                caption_body = content.get("caption", "")
                for i, (name, peer_id) in enumerate(recipient_items, start=1):
                    if job.stop:
                        break
                    limited = False
                    try:
                        res = None
                        used_bridge = False
                        # Fast path: send via Eitaa's own engine using peer_id.
                        # Text -> __MKWL_send; file -> reuse the once-uploaded doc.
                        if peer_id:
                            b = None
                            if is_file and file_bridge_ready:
                                b = await driver.bridge_file_send(peer_id, caption_body)
                            elif not is_file:
                                b = await driver.bridge_send(peer_id, text_body)
                            if b is not None:
                                if b.get("limit"):
                                    # Server itself reported a flood/limit.
                                    await report(cards.restriction_card(
                                        account, f"server: {b.get('code')}", sent))
                                    limited = True
                                elif b.get("ok"):
                                    used_bridge = True
                                    res = SendResult(
                                        ok=True, to=name,
                                        detail=f"bridge/{b.get('method')} id={b.get('msg_id')}")
                                else:
                                    print(f"[send] bridge miss for {name[:18]!r} "
                                          f"({b.get('code')}); UI fallback", flush=True)
                        # Fallback: proven UI send (covers no-peer_id, a bridge
                        # miss, or an unavailable file bridge).
                        if not limited and res is None:
                            if is_file:
                                res = await driver.send_file(
                                    content.get("file_path", ""),
                                    caption=caption_body, query=name)
                            else:
                                res = await driver.send_text(name, text_body, verify=True)

                        if limited:
                            # A server-reported limit was already surfaced above;
                            # do not count it as a per-send failure.
                            pass
                        elif res is not None and res.ok:
                            sent += 1
                            consecutive_failures = 0
                            if used_bridge:
                                via_bridge += 1
                            else:
                                via_fallback += 1
                        else:
                            failed += 1
                            consecutive_failures += 1
                            detail = res.detail if res is not None else "send produced no result"
                            # Surface EXACTLY why the send failed (capped to
                            # avoid spam; the brake stops us after a few anyway).
                            if error_cards < 12:
                                await report(cards.error_card(
                                    where, account, target=name, code="send_failed",
                                    detail=detail, trace_id=job.job_id))
                                error_cards += 1
                            if _is_limit(detail):
                                await report(cards.restriction_card(account, detail, sent))
                                limited = True
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        consecutive_failures += 1
                        if error_cards < 12:
                            await report(cards.error_card(
                                where, account, target=name,
                                code=type(exc).__name__, detail=str(exc),
                                trace_id=job.job_id))
                            error_cards += 1
                    if limited:
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

                    await asyncio.sleep(delay)

                # Evidence of which path carried the load (fast bridge vs the UI
                # fallback) so alternative methods stay observable at a glance.
                print(f"[send] path summary: via_bridge={via_bridge} "
                      f"via_fallback={via_fallback} sent={sent} failed={failed} "
                      f"({'file' if is_file else 'text'})", flush=True)
                job.summary = {"sent": sent, "failed": failed, "skipped": skipped,
                               "total": total, "via_bridge": via_bridge,
                               "via_fallback": via_fallback}
                await self._send_progress(
                    live, agg, account, phone, sent=sent, failed=failed,
                    total=total, elapsed=time.time() - start,
                    status="🛑 Stopped" if job.stop else "✅ Done",
                    state="stopped" if job.stop else "done",
                    engine=engine, kind=kind, force=True)
                # In a multi-account run the combined summary is posted once by
                # the supervisor, so skip the per-account finish card.
                if agg is None:
                    await report(cards.send_finished(
                        account, kind, sent, failed, skipped, total,
                        time.time() - start, stopped=job.stop))
        except Exception as exc:  # noqa: BLE001
            if agg is not None:
                await agg.update(account, state="failed", force=True)
            await report(cards.error_card("send_job", account, code=type(exc).__name__,
                                          detail=str(exc), trace_id=job.job_id))
        finally:
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
                    detail="no saved peers for this account. Build contacts (or run a "
                           "send once with the bridge engine) so peers get harvested, "
                           "or switch the engine to bridge."))
                if agg is not None:
                    await agg.update(account, state="failed", force=True)
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

                await asyncio.sleep(delay)

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
        engine = str(settings.get("engine", config.ENGINE))
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
            async with open_session(account) as session:
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
                        await asyncio.sleep(delay)
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
                        await asyncio.sleep(delay)

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
            while done < total:
                if job.stop:
                    break
                batch = entries[done:done + BATCH]
                # Blocking HTTPS in a worker thread so the panel stays responsive.
                r = await asyncio.to_thread(sender.import_contacts, batch)
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
                else:
                    imp = int(r.get("imported", 0))
                    added += imp
                    not_on += max(0, len(batch) - imp)
                done += len(batch)
                if live is not None:
                    await live.set(cards.live_contacts(
                        phone, prefix, added, done, total, status="🟢 Searching",
                        engine="direct", not_on=not_on, failed=error))
                await asyncio.sleep(delay)
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
