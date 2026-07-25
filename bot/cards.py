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
        # Sanitize line BY line: sanitize() collapses all whitespace, which would
        # otherwise flatten a multi-line body (e.g. a numbered queue) into one row.
        lines.extend(sanitize(ln, 300) for ln in str(body).splitlines())
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


def bar(done: int, total: int, width: int = 12) -> str:
    """A text progress bar: `▰▰▰▰▱▱▱▱▱▱▱▱ 33%`.

    Gives the live cards something that visibly moves on every edit, which is
    the whole point of a card that updates in place.
    """
    if total <= 0:
        return "▱" * width + "  0%"
    ratio = min(1.0, max(0.0, done / total))
    filled = int(round(ratio * width))
    # Never show a full bar until it really is finished.
    if filled == width and done < total:
        filled = width - 1
    return "▰" * filled + "▱" * (width - filled) + f"  {int(ratio * 100)}%"


def eta(done: int, total: int, elapsed: float) -> str:
    """Rough remaining time from the average pace so far."""
    if done <= 0 or total <= done or elapsed <= 0:
        return "—"
    return fmt_duration((elapsed / done) * (total - done))


def _live(title: str, phone: str, pairs: Iterable[tuple[str, object]], ts: str | None = None) -> str:
    """Live-card shell: title, divider, 📱 phone, aligned rows, 🕒 time.
    This is the card that gets EDITED IN PLACE while a job runs."""
    lines = [title, DIVIDER, f"📱 {phone}", *_rows(pairs), f"🕒 {ts or now_hms()}"]
    return "\n".join(lines)


# ---- LIVE cards (edited in place while a job runs) ---------------------

def live_contacts(phone: str, prefix: str, found: int, probed: int, total: int,
                  status: str = "🟢 Searching", engine: str | None = None,
                  not_on: int | None = None, failed: int | None = None) -> str:
    lines = [
        "🔎 BUILDING CONTACTS — Live",
        DIVIDER,
        f"📱 {phone}",
        bar(probed, total),
        *_rows([
            ("Status ", status),
            ("Prefix ", prefix),
            ("Found  ", f"✅ {found}"),
            ("Checked", f"{probed} of {total}"),
            ("Off App", not_on or None),
            ("Failed ", failed or None),
        ]),
        f"🕒 {now_hms()}",
    ]
    return "\n".join(lines)


def live_send(phone: str, sent: int, failed: int, total: int, elapsed: float,
              status: str = "🟢 Sending", engine: str | None = None,
              kind: str | None = None) -> str:
    lines = [
        "📤 SENDING — Live",
        DIVIDER,
        f"📱 {phone}",
        bar(sent + failed, total),
        *_rows([
            ("Status ", status),
            ("Type   ", kind),
            ("Sent   ", f"{sent} of {total}"),
            ("Failed ", failed or None),
            ("Elapsed", fmt_duration(elapsed)),
            ("Left   ", eta(sent + failed, total, elapsed)),
        ]),
        f"🕒 {now_hms()}",
    ]
    return "\n".join(lines)


# Per-account state marks used in the multi-account breakdown.
_STATE_MARK = {
    "pending": "⏳",
    "running": "🟢",
    "done": "✅",
    "stopped": "🛑",
    "failed": "⚠️",
    "limited": "🚫",
    "no_targets": "🚧",
    "preparing": "📥",
}

_STATE_WORD = {
    "done": "finished",
    "stopped": "stopped",
    "failed": "failed",
    "limited": "hit a limit",
    "no_targets": "had no targets",
    "preparing": "reading contacts",
    "running": "sending",
    "pending": "waiting",
}


def _account_tally(accounts: list[dict]) -> str:
    """`8 · ✅1 🚧7` — so accounts that could not send are never hidden behind a
    combined percentage."""
    states = [str(a.get("state", "pending")) for a in accounts]
    ok = sum(1 for s in states if s in ("done", "running", "stopped"))
    blocked = states.count("no_targets")
    failed = states.count("failed") + states.count("limited")
    out = str(len(accounts))
    if ok:
        out += f" · ✅{ok}"
    if blocked:
        out += f" · 🚧{blocked} no peers"
    if failed:
        out += f" · ⚠️{failed}"
    return out


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
    lines = [
        "🚀 MULTI SEND — Live",
        DIVIDER,
        bar(sent + failed, total),
        *_rows([
            ("Status  ", status),
            ("Type    ", kind),
            ("Accounts", _account_tally(accounts)),
            ("Now     ", current or "—"),
            ("Sent    ", f"{sent} of {total}"),
            ("Failed  ", failed or None),
            ("Elapsed ", fmt_duration(elapsed)),
            ("Left    ", eta(sent + failed, total, elapsed)),
        ]),
    ]
    if accounts:
        lines.append(DIVIDER)
        # Numbered, because the run is sequential: 1 finishes, then 2, then 3.
        width = max(len(str(a.get("phone", ""))) for a in accounts)
        for i, a in enumerate(accounts, start=1):
            state = str(a.get("state", "pending"))
            mark = _STATE_MARK.get(state, "•")
            phone = str(a.get("phone", "")).ljust(width)
            row = f"{i}. {mark} {phone} · {a.get('sent', 0)}/{a.get('total', 0)}"
            if a.get("failed"):
                row += f" · ✗{a['failed']}"
            lines.append(row)
    lines.append(f"🕒 {now_hms()}")
    return "\n".join(lines)


def multi_ready(accounts: list[dict], total: int, kind: str | None = None) -> str:
    """Posted once the reach of every ticked account is known, before sending.

    This is the "sum it up first" card: each account's contact count and the
    grand total, in the exact order they will be used.
    """
    lines = ["🧾 MULTI SEND — QUEUE READY", DIVIDER]
    width = max((len(str(a.get("phone", ""))) for a in accounts), default=0)
    for i, a in enumerate(accounts, start=1):
        phone = str(a.get("phone", "")).ljust(width)
        n = int(a.get("total", 0) or 0)
        lines.append(f"{i}. {phone} · {n:,} contacts" + ("" if n else "  ⚠️ none"))
    lines += [
        DIVIDER,
        *_rows([
            ("Accounts", len(accounts)),
            ("Type    ", kind),
            ("TOTAL   ", f"{total:,} messages to send"),
        ]),
        DIVIDER,
        f"Starting with #1. Each account runs to the end (or until it stops or "
        f"fails), then the next one begins.",
        f"🕒 {now_hms()}",
    ]
    return "\n".join(lines)


def multi_account_done(phone: str, order: int, of: int, state: str, sent: int,
                       failed: int, total: int, next_phone: str | None = None) -> str:
    """Per-account result inside a sequential multi run, plus what comes next."""
    mark = _STATE_MARK.get(state, "•")
    word = _STATE_WORD.get(state, state)
    lines = [
        f"{mark} ACCOUNT {order}/{of} {word.upper()}",
        DIVIDER,
        f"📱 {phone}",
        bar(sent + failed, total),
        *_rows([
            ("Sent  ", f"✅ {sent} of {total}"),
            ("Failed", f"✗ {failed}" if failed else None),
        ]),
        DIVIDER,
        (f"➡️ Moving on to account {order + 1}/{of}: {next_phone}"
         if next_phone else "No accounts left in the queue."),
        f"🕒 {now_hms()}",
    ]
    return "\n".join(lines)


# ---- ready-made cards --------------------------------------------------

def _ping_mark(ping_ms: int | None) -> str:
    if ping_ms is None:
        return "🔴 unreachable"
    if ping_ms < 300:
        return f"🟢 {ping_ms} ms"
    if ping_ms < 1000:
        return f"🟡 {ping_ms} ms"
    return f"🔴 {ping_ms} ms"


def panel_home(version: str, accounts: int, active: str | None,
               engine: str | None = None, ping_ms: int | None = None,
               bot_online: bool = True, contacts: int | None = None,
               running: int = 0, content: str | None = None) -> str:
    """Home screen: what's connected, what's loaded, what's running."""
    lines = [
        "🤖 EITAA MANAGER",
        DIVIDER,
        *_rows([
            ("Bot     ", "🟢 online" if bot_online else "🔴 offline"),
            ("Eitaa   ", _ping_mark(ping_ms)),
            ("Accounts", accounts if accounts else "none yet"),
            ("Contacts", f"{contacts:,}" if contacts else None),
            ("Active  ", active or "—"),
            ("Running ", f"⏳ {running} job(s)" if running else "idle"),
            ("Content ", content),
            ("Version ", version),
        ]),
        DIVIDER,
        "Pick a section below.",
    ]
    return "\n".join(lines)


def account_added(account: str, phone: str, contacts: int | None, pvs: int | None,
                  engine: str | None = None, saved: int | None = None) -> str:
    return card(
        "✅ ACCOUNT ADDED",
        [
            ("Phone   ", phone),
            ("Contacts", f"{contacts:,}" if isinstance(contacts, int) and contacts >= 0 else "—"),
            ("Chats   ", pvs if isinstance(pvs, int) and pvs >= 0 else "—"),
            ("Saved   ", f"{saved:,}" if saved else None),
            ("Time    ", now_hms()),
        ],
        footer=("Contacts saved — this account is ready to send." if saved else
                "Open the account and tap 📥 Save Contacts to make sends start instantly."),
    )


def account_panel(account: str, phone: str, contacts: int | None, pvs: int | None,
                  engine: str | None, busy: bool, peers: int | None = None,
                  saved: int | None = None, saved_age: float | None = None) -> str:
    """One account's panel.

    `saved` is how many contacts are in the local cache -- that is what a send
    actually iterates, so it is the number that matters most here. `contacts` is
    what Eitaa reported at the last refresh.
    """
    age = None
    if saved:
        if saved_age is None:
            age = "just now"
        elif saved_age < 1:
            age = "under an hour ago"
        elif saved_age < 48:
            age = f"{int(saved_age)}h ago"
        else:
            age = f"{int(saved_age / 24)}d ago"

    if busy:
        footer = "A job is running on this account. Use Stop to end it early."
    elif not saved:
        footer = ("No contacts saved yet. Tap 📥 Save Contacts once — after that every "
                  "send starts instantly instead of re-scrolling the list.")
    else:
        footer = f"Ready to send to {saved:,} saved contact(s)."

    return card(
        "👤 ACCOUNT",
        [
            ("Phone    ", phone),
            ("State    ", "⏳ busy" if busy else "🟢 idle"),
            ("Saved    ", f"{saved:,} contacts ({age})" if saved else "none"),
            ("On Eitaa ", f"{contacts:,}" if isinstance(contacts, int) and contacts >= 0 else "—"),
            ("Chats    ", pvs if isinstance(pvs, int) and pvs >= 0 else "—"),
            # Only meaningful while the browser-free engine is enabled.
            ("Peers    ", peers if (peers and engine == "direct") else None),
        ],
        footer=footer,
    )


def contacts_saved(phone: str, count: int, with_peer: int, elapsed: float,
                   replaced: int | None = None) -> str:
    """Result of caching an account's contacts list."""
    pairs = [
        ("Phone     ", phone),
        ("Saved     ", f"{count:,} contacts"),
        ("With id   ", f"{with_peer:,}"),
        ("Time      ", fmt_duration(elapsed)),
    ]
    if replaced is not None and replaced != count:
        pairs.insert(2, ("Previously", f"{replaced:,}"))
    return card(
        "📥 CONTACTS SAVED" if count else "📥 NO CONTACTS FOUND",
        pairs,
        footer=("Sends from this account now start immediately." if count else
                "The contacts list came back empty. Open Eitaa's Contacts view once, "
                "then try again."),
    )


def send_started(account: str, kind: str, targets: int, delay: float) -> str:
    return card(
        "📤 SEND STARTED",
        [
            ("Phone  ", account),
            ("Type   ", kind),
            ("Targets", f"{targets:,}"),
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
    if stopped:
        title = "🛑 SEND STOPPED"
    elif failed and not sent:
        title = "⚠️ SEND FAILED"
    elif failed:
        title = "⚠️ SEND FINISHED (with failures)"
    else:
        title = "✅ SEND FINISHED"
    lines = [
        title,
        DIVIDER,
        f"📱 {account}",
        bar(sent + failed, total),
        *_rows([
            ("Type   ", kind),
            ("Sent   ", f"✅ {sent} of {total}"),
            ("Failed ", f"✗ {failed}" if failed else None),
            ("Skipped", skipped or None),
            ("Time   ", fmt_duration(elapsed)),
        ]),
        f"🕒 {now_hms()}",
    ]
    return "\n".join(lines)


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
    if stopped:
        title = "🛑 CONTACT BUILD STOPPED"
    elif added:
        title = "✅ CONTACT BUILD FINISHED"
    else:
        title = "⚠️ CONTACT BUILD FINISHED (nothing added)"
    lines = [
        title,
        DIVIDER,
        f"📱 {account}",
        bar(added + not_on + invalid + error, total),
        *_rows([
            ("Added       ", f"✅ {added}"),
            ("Not on Eitaa", not_on or None),
            ("Invalid     ", invalid or None),
            ("Error       ", error or None),
            ("Checked     ", total),
            ("Time        ", fmt_duration(elapsed)),
        ]),
        f"🕒 {now_hms()}",
    ]
    if not added:
        lines.append("None of these numbers are registered on Eitaa. "
                     "Try a different prefix.")
    return "\n".join(lines)


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
                   fallback: bool = False, note: str | None = None) -> str:
    """Report EXACTLY what the server answered for the first import batch.

    Contact building used to report "0 found" with no explanation when the
    server matched nothing, which looked like the job doing nothing at all.
    Each probe row is one phone format we tried, with the raw counts.

    tried: [{"format": "98"|"+98", "imported": int, "users": int,
             "retry": int, "batch": int, "code": str|None}]
    """
    rows = []
    for t in tried:
        label = f"Format {t.get('format', '?')}"
        if t.get("code"):
            rows.append((label, f"error: {sanitize(t['code'], 90)}"))
            continue
        detail = (f"imported={t.get('imported', 0)} users={t.get('users', 0)} "
                  f"retry={t.get('retry', 0)} of {t.get('batch', 0)}")
        # An unexpected reply constructor also looks like "imported 0", so name it.
        if t.get("parse_ok") is False:
            cid = t.get("cid")
            detail += f" | UNEXPECTED REPLY cid={('0x%08x' % cid) if cid else '?'}"
            if t.get("head"):
                detail += f" head={t['head'][:32]}"
        rows.append((label, detail))
    footer = note
    if note:
        pass
    elif chosen:
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
    blocked = [a for a in accounts if str(a.get("state")) == "no_targets"]
    bad = [a for a in accounts
           if str(a.get("state")) in ("no_targets", "failed", "limited")]
    if stopped:
        title = "🛑 MULTI SEND STOPPED"
    elif bad:
        # Honest title: some accounts did not deliver everything.
        title = f"⚠️ MULTI SEND FINISHED — {len(bad)} of {len(accounts)} had problems"
    else:
        title = "✅ MULTI SEND FINISHED"
    lines = [
        title,
        DIVIDER,
        bar(sent + failed, total),
        *_rows([
            ("Accounts", _account_tally(accounts)),
            ("Type    ", kind),
            ("Sent    ", f"✅ {sent} of {total}"),
            ("Failed  ", f"✗ {failed}" if failed else None),
            ("Time    ", fmt_duration(elapsed)),
        ]),
    ]
    if accounts:
        lines.append(DIVIDER)
        for i, a in enumerate(accounts, start=1):
            state = str(a.get("state", "done"))
            mark = _STATE_MARK.get(state, "•")
            row = (f"{i}. {mark} {a.get('phone', '')} · "
                   f"{a.get('sent', 0)}/{a.get('total', 0)}")
            if a.get("failed"):
                row += f" · ✗{a['failed']}"
            if state in ("no_targets", "failed", "limited", "stopped"):
                row += f" · {_STATE_WORD.get(state, state)}"
            lines.append(row)
    lines.append(DIVIDER)
    if blocked:
        lines.append(f"🚧 {len(blocked)} account(s) sent NOTHING because they have no "
                     "saved peers. Open each one and tap '📥 Save Contacts', then send "
                     "again.")
    lines.append(f"🕒 {now_hms()}")
    return "\n".join(lines)




