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

    async def run_login(self, account: str, report: Report, novnc_url: str = "") -> bool:
        """Start an interactive login for a (possibly new) account.

        Opens a headed Eitaa Web session the owner completes on their noVNC
        screen, then auto-detects success and saves the profile. Returns False
        if the account is already busy with another job.
        """
        if account in self._busy:
            return False
        self._busy.add(account)
        asyncio.create_task(self._login_job(account, report, novnc_url))
        return True

    async def _login_job(self, account: str, report: Report, novnc_url: str) -> None:
        # Local imports keep this module importable without a browser present.
        from capture.browser import open_session
        from eitaa.driver import EitaaDriver
        try:
            # Headed so the owner can complete phone+code on the noVNC screen.
            async with open_session(account, headed=True) as session:
                await session.goto()
                driver = EitaaDriver(session)
                await driver.open()

                if await driver.is_logged_in():
                    await report(cards.card(
                        "👤 ACCOUNT READY",
                        [("Account", account), ("Status", "already logged in")],
                        footer="This account is already logged in and ready to use."))
                    return

                hint = (f"Open noVNC: {novnc_url}" if novnc_url
                        else "Open your noVNC screen (browser display :99)")
                await report(cards.card(
                    "👤 LOGIN STARTED",
                    [("Account", account), ("Waiting", "up to 6 min")],
                    footer=f"{hint} and log in (phone + code). I'll detect it and "
                           f"save automatically. Never share your login code."))

                deadline = time.time() + 360
                ok = False
                while time.time() < deadline:
                    await asyncio.sleep(4)
                    try:
                        if await driver.is_logged_in():
                            ok = True
                            break
                    except Exception:  # noqa: BLE001
                        pass

                if ok:
                    # Let Eitaa Web persist its auth to the profile before close.
                    await asyncio.sleep(2)
                    await report(cards.card(
                        "✅ ACCOUNT ADDED",
                        [("Account", account)],
                        footer="Login detected and saved. It now appears under Accounts."))
                else:
                    await report(cards.card(
                        "⌛ LOGIN TIMEOUT",
                        [("Account", account)],
                        footer="No login detected in time. Tap Add Account to try again."))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card(
                "login", account, code=type(exc).__name__, detail=str(exc)))
        finally:
            self._busy.discard(account)

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
                    await report(cards.card(
                        "✅ ACCOUNT ADDED",
                        [("Account", account), ("Phone", intl)],
                        footer="Logged in via the bridge (no noVNC). It now appears under Accounts."))
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
                await report(cards.send_started(account, kind, total, delay))

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

                    if i % log_every == 0:
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

                if await driver.ensure_contacts_bridge():
                    # FAST PATH: contacts.importContacts in batches. The server
                    # returns exactly which numbers are on Eitaa (as users WITH
                    # access_hash -> instantly sendable). No per-number UI popup,
                    # no server strain.
                    BATCH = 50
                    done = 0
                    while done < total:
                        if job.stop:
                            break
                        batch = entries[done:done + BATCH]
                        r = await driver.bridge_import_contacts(batch)
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
                        else:
                            error += len(batch)
                            await report(cards.error_card(
                                "import_contacts", account, code="import_failed",
                                detail=str(r.get("code")), trace_id=job.job_id))
                        done += len(batch)
                        await report(cards.contacts_progress(
                            added, not_on, invalid, error, total - done))
                        await asyncio.sleep(delay)
                    print(f"[contacts] path=bridge added={added} not_on={not_on} "
                          f"error={error} of {total}", flush=True)
                else:
                    # FALLBACK: the proven (slower) per-number UI add flow.
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
                    print(f"[contacts] path=UI added={added} not_on={not_on} "
                          f"error={error} of {total}", flush=True)

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
