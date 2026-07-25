"""English styled log/panel cards (ReconBot style).

Every card uses the same shell:

    <emoji> <TITLE>
    -------------------------------
    • Key   : value
    • Key   : value
    -------------------------------
    [footer]

Keep all card text English. Values coming from Eitaa (error messages, names)
must be passed through `sanitize()` before being placed in a card so we never
leak secrets or break the layout.
"""

from __future__ import annotations

import re
import time
from typing import Iterable

DIVIDER = "-------------------------------"

# Redact obvious secrets if they ever appear in an error string.
_SECRET_RE = re.compile(
    r"(?i)(token|api[_-]?hash|api[_-]?id|password|passwd|secret|authorization|bearer|session)"
    r"\s*[:=]\s*\S+"
)


def sanitize(text: str, limit: int = 300) -> str:
    """Make an arbitrary string safe for a one-line card row."""
    if text is None:
        return ""
    s = str(text)
    s = _SECRET_RE.sub(r"\1: <redacted>", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _rows(pairs: Iterable[tuple[str, object]]) -> list[str]:
    """Format key/value pairs with aligned keys as `• Key   : value`."""
    pairs = [(str(k), v) for k, v in pairs if v is not None]
    if not pairs:
        return []
    width = max(len(k) for k, _ in pairs)
    return [f"• {k.ljust(width)} : {v}" for k, v in pairs]


def card(title: str, pairs: Iterable[tuple[str, object]] | None = None,
         footer: str | None = None, body: str | None = None) -> str:
    """Build a card. `pairs` are key/value rows; `body`/`footer` are free text."""
    lines = [title, DIVIDER]
    if body:
        lines.append(sanitize(body, 800))
    rows = _rows(pairs or [])
    if rows:
        lines.extend(rows)
    lines.append(DIVIDER)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def now_hms() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _pct(done: int, total: int) -> str:
    if not total:
        return "0%"
    return f"{int(done * 100 / total)}%"


def _live(title: str, phone: str, pairs: Iterable[tuple[str, object]], ts: str | None = None) -> str:
    """Live-card shell: title, divider, 📱 phone, aligned rows, 🕒 time.
    This is the card that gets EDITED IN PLACE while a job runs."""
    lines = [title, DIVIDER, f"📱 {phone}", *_rows(pairs), f"🕒 {ts or now_hms()}"]
    return "\n".join(lines)


# ---- LIVE cards (edited in place while a job runs) ---------------------

def live_contacts(phone: str, prefix: str, found: int, probed: int, total: int,
                  status: str = "🟢 Searching", engine: str | None = None,
                  not_on: int | None = None, failed: int | None = None) -> str:
    return _live(
        "🔎 Discover Friends by Prefix — Live",
        phone,
        [
            ("Prefix ", prefix),
            ("Engine ", engine),
            ("Status ", status),
            ("Found  ", f"{found} of {total} — {_pct(probed, total)}"),
            ("Probed ", probed),
            ("Off App", not_on),
            ("Failed ", failed),
        ],
    )


def live_send(phone: str, sent: int, failed: int, total: int, elapsed: float,
              status: str = "🟢 Sending", engine: str | None = None,
              kind: str | None = None) -> str:
    return _live(
        "📤 Send to Contacts — Live",
        phone,
        [
            ("Engine ", engine),
            ("Type   ", kind),
            ("Status ", status),
            ("Sent   ", f"{sent} of {total} — {_pct(sent, total)}"),
            ("Failed ", failed),
            ("Elapsed", fmt_duration(elapsed)),
        ],
    )


# Per-account state marks used in the multi-account breakdown.
_STATE_MARK = {
    "pending": "⏳",
    "running": "🟢",
    "done": "✅",
    "stopped": "🛑",
    "failed": "⚠️",
    "limited": "🚫",
}


def live_send_multi(accounts: list[dict], current: str | None, sent: int,
                    failed: int, total: int, elapsed: float,
                    status: str = "🟢 Sending", engine: str | None = None,
                    kind: str | None = None) -> str:
    """ONE live card for a simultaneous multi-account send.

    `sent`/`failed`/`total` are the COMBINED numbers across every selected
    account (their contact lists added together), so the top block answers "how
    much of the whole job is done". `current` names the account that most
    recently sent, and the breakdown block shows each account's own progress.

    accounts: [{"phone": str, "sent": int, "failed": int, "total": int,
                "state": "pending|running|done|stopped|failed|limited"}]
    """
    head = _rows([
        ("Accounts", len(accounts)),
        ("Current ", current or "—"),
        ("Engine  ", engine),
        ("Type    ", kind),
        ("Status  ", status),
        ("Sent    ", f"{sent} of {total} — {_pct(sent, total)}"),
        ("Failed  ", failed),
        ("Elapsed ", fmt_duration(elapsed)),
    ])
    lines = ["📤 Multi-Account Send — Live", DIVIDER, *head]
    if accounts:
        lines.append(DIVIDER)
        width = max(len(str(a.get("phone", ""))) for a in accounts)
        for a in accounts:
            mark = _STATE_MARK.get(str(a.get("state", "pending")), "•")
            phone = str(a.get("phone", "")).ljust(width)
            row = (f"{mark} {phone} · {a.get('sent', 0)}/{a.get('total', 0)}")
            if a.get("failed"):
                row += f" · ✗{a['failed']}"
            lines.append(row)
    lines.append(f"🕒 {now_hms()}")
    return "\n".join(lines)


# ---- ready-made cards --------------------------------------------------

def panel_home(version: str, accounts: int, active: str | None,
               engine: str | None = None, ping_ms: int | None = None,
               bot_online: bool = True) -> str:
    ping = f"{ping_ms} ms" if ping_ms is not None else "—"
    rows = _rows([
        ("Bot    ", "🟢 Online" if bot_online else "🔴 Offline"),
        ("Engine ", ("🌉 bridge (browser)" if engine == "bridge"
                     else "⚡ direct (no browser)") if engine else None),
        ("Server ", f"🛰 {ping}"),
        ("Version", version),
        ("Accounts", accounts),
        ("Active ", active or "none"),
    ])
    lines = [
        "🤖 EitaaManager",
        DIVIDER,
        "• Multi-Account Eitaa Manager",
        *rows,
        DIVIDER,
        "Choose an action.",
    ]
    return "\n".join(lines)


def account_added(account: str, phone: str, contacts: int | None, pvs: int | None,
                  engine: str | None = None) -> str:
    return card(
        "✅ ACCOUNT ADDED",
        [
            ("Account ", account),
            ("Phone   ", phone),
            ("Contacts", contacts if contacts is not None else "—"),
            ("Chats   ", pvs if pvs is not None else "—"),
            ("Engine  ", engine),
            ("Time    ", now_hms()),
        ],
    )


def account_panel(account: str, phone: str, contacts: int | None, pvs: int | None,
                  engine: str | None, busy: bool, peers: int | None = None) -> str:
    """One account's panel. `peers` is how many contacts the browser-free (fast)
    sender can reach, which is the difference between a direct send working and
    having no targets at all."""
    footer = "Send content or build contacts with this account."
    if engine == "direct" and not peers:
        footer = ("No saved peers yet, so the fast engine has no targets. Build "
                  "contacts (or send once with the bridge engine) to harvest them.")
    return card(
        "👤 ACCOUNT",
        [
            ("Phone   ", phone),
            ("Contacts", contacts if contacts is not None else "—"),
            ("Chats   ", pvs if pvs is not None else "—"),
            ("Peers   ", peers if peers is not None else "—"),
            ("Engine  ", engine),
            ("State   ", "⏳ busy" if busy else "🟢 idle"),
        ],
        footer=footer,
    )


def send_started(account: str, kind: str, targets: int, delay: float) -> str:
    return card(
        "📤 SEND STARTED",
        [
            ("Account", account),
            ("Type   ", kind),
            ("Targets", targets),
            ("Delay  ", f"{delay:g}s"),
            ("Time   ", now_hms()),
        ],
    )


def send_progress(sent: int, failed: int, skipped: int, left: int, elapsed: float) -> str:
    return card(
        "📈 PROGRESS",
        [
            ("Sent   ", sent),
            ("Failed ", failed),
            ("Skipped", skipped),
            ("Left   ", left),
            ("Elapsed", fmt_duration(elapsed)),
        ],
    )


def send_finished(account: str, kind: str, sent: int, failed: int, skipped: int,
                  total: int, elapsed: float, stopped: bool = False) -> str:
    return card(
        "🛑 SEND STOPPED" if stopped else "✅ SEND FINISHED",
        [
            ("Account", account),
            ("Type   ", kind),
            ("Sent   ", sent),
            ("Failed ", failed),
            ("Skipped", skipped),
            ("Total  ", total),
            ("Time   ", fmt_duration(elapsed)),
        ],
    )


def contacts_started(account: str, prefix: str, count: int, delay: float) -> str:
    return card(
        "👤 CONTACT BUILD STARTED",
        [
            ("Account", account),
            ("Prefix ", prefix),
            ("Planned", count),
            ("Delay  ", f"{delay:g}s"),
            ("Time   ", now_hms()),
        ],
    )


def contacts_progress(added: int, not_on: int, invalid: int, error: int, left: int) -> str:
    return card(
        "📈 CONTACT PROGRESS",
        [
            ("Added      ", added),
            ("Not on Eitaa", not_on),
            ("Invalid    ", invalid),
            ("Error      ", error),
            ("Left       ", left),
        ],
    )


def contacts_finished(account: str, added: int, not_on: int, invalid: int,
                      error: int, total: int, elapsed: float, stopped: bool = False) -> str:
    return card(
        "🛑 CONTACT BUILD STOPPED" if stopped else "✅ CONTACT BUILD FINISHED",
        [
            ("Account     ", account),
            ("Added       ", added),
            ("Not on Eitaa", not_on),
            ("Invalid     ", invalid),
            ("Error       ", error),
            ("Total       ", total),
            ("Time        ", fmt_duration(elapsed)),
        ],
    )


def error_card(where: str, account: str | None = None, target: str | None = None,
               code: str | None = None, detail: str | None = None,
               trace_id: str | None = None, engine: str | None = None,
               phase: str | None = None) -> str:
    return card(
        "⚠️ ERROR",
        [
            ("Trace  ", trace_id),
            ("Where  ", where),
            ("Phase  ", phase),
            ("Engine ", engine),
            ("Account", account),
            ("Target ", sanitize(target, 60) if target else None),
            ("Code   ", code),
            ("Detail ", sanitize(detail, 400) if detail else None),
            ("Time   ", now_hms()),
        ],
    )


def restriction_card(account: str, reason: str, sent_before: int) -> str:
    return card(
        "🚫 LIMIT DETECTED",
        [
            ("Account    ", account),
            ("Reason     ", sanitize(reason, 200)),
            ("Sent before", sent_before),
            ("Action     ", "job auto-paused"),
            ("Time       ", now_hms()),
        ],
    )


def paused_card(account: str, reason: str, sent_before: int) -> str:
    """Job auto-paused by the safety brake (not necessarily a real limit)."""
    return card(
        "⏸ SEND PAUSED",
        [
            ("Account    ", account),
            ("Reason     ", sanitize(reason, 200)),
            ("Sent before", sent_before),
            ("Action     ", "auto-paused; see error cards above"),
            ("Time       ", now_hms()),
        ],
    )



def contacts_probe(account: str, tried: list[dict], chosen: str | None,
                   fallback: bool = False) -> str:
    """Report EXACTLY what the server answered for the first import batch.

    Contact building used to report "0 found" with no explanation when the
    server matched nothing, which looked like the job doing nothing at all.
    Each probe row is one phone format we tried, with the raw counts.

    tried: [{"format": "98"|"+98", "imported": int, "users": int,
             "retry": int, "batch": int, "code": str|None}]
    """
    rows = []
    for t in tried:
        if t.get("code"):
            rows.append((f"Format {t.get('format', '?')}", f"error: {sanitize(t['code'], 90)}"))
        else:
            rows.append((
                f"Format {t.get('format', '?')}",
                f"imported={t.get('imported', 0)} users={t.get('users', 0)} "
                f"retry={t.get('retry', 0)} of {t.get('batch', 0)}",
            ))
    footer = None
    if chosen:
        footer = f"Using phone format {chosen} for the rest of this job."
    elif fallback:
        footer = ("Neither phone format matched anyone, so the job switched to the "
                  "proven one-by-one add flow. If that also finds nobody, these "
                  "numbers are simply not registered on Eitaa.")
    return card(
        "🔬 IMPORT PROBE",
        [("Account", account), *rows, ("Time   ", now_hms())],
        footer=footer,
    )


def peers_saved(account: str, new_peers: int, total_peers: int,
                source: str = "import") -> str:
    """Peers are what the browser-free (fast) sender needs to reach a contact."""
    return card(
        "🔑 PEERS SAVED",
        [
            ("Account", account),
            ("Source ", source),
            ("New    ", new_peers),
            ("Total  ", total_peers),
            ("Time   ", now_hms()),
        ],
        footer="These contacts can now be reached with the fast (no-browser) sender.",
    )


def account_deleted(phone: str, removed: list[str]) -> str:
    return card(
        "🗑 ACCOUNT DELETED",
        [
            ("Phone  ", phone),
            ("Removed", ", ".join(removed) if removed else "nothing found"),
            ("Time   ", now_hms()),
        ],
        footer="Its browser profile, saved peers and captured session are gone.",
    )


def multi_send_finished(accounts: list[dict], sent: int, failed: int, total: int,
                        elapsed: float, kind: str | None = None,
                        engine: str | None = None, stopped: bool = False) -> str:
    """Final summary of a multi-account send (combined + per account)."""
    lines = [
        "🛑 MULTI-ACCOUNT SEND STOPPED" if stopped else "✅ MULTI-ACCOUNT SEND FINISHED",
        DIVIDER,
        *_rows([
            ("Accounts", len(accounts)),
            ("Engine  ", engine),
            ("Type    ", kind),
            ("Sent    ", f"{sent} of {total}"),
            ("Failed  ", failed),
            ("Time    ", fmt_duration(elapsed)),
        ]),
    ]
    if accounts:
        lines.append(DIVIDER)
        for a in accounts:
            mark = _STATE_MARK.get(str(a.get("state", "done")), "•")
            lines.append(f"{mark} {a.get('phone', '')} · "
                         f"{a.get('sent', 0)}/{a.get('total', 0)}"
                         + (f" · ✗{a['failed']}" if a.get("failed") else ""))
    lines.append(DIVIDER)
    lines.append(f"🕒 {now_hms()}")
    return "\n".join(lines)
