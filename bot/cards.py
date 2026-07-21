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


# ---- ready-made cards --------------------------------------------------

def panel_home(version: str, accounts: int, active: str | None) -> str:
    rows = _rows([
        ("Status  ", "🟢 Online"),
        ("Version ", version),
        ("Accounts", accounts),
        ("Active  ", active or "none"),
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
               trace_id: str | None = None) -> str:
    return card(
        "⚠️ ERROR",
        [
            ("Trace ", trace_id),
            ("Where ", where),
            ("Account", account),
            ("Target", sanitize(target, 60) if target else None),
            ("Code  ", code),
            ("Detail", sanitize(detail, 400) if detail else None),
            ("Time  ", now_hms()),
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
