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
    """Format key/value pairs as `• Key: value`.

    Keys are NOT padded to a common width any more. Telegram renders card text
    in a proportional font, so space padding does not line anything up -- on a
    phone it just produced ragged rows with random gaps. Callers may still pass
    padded labels (many do); the padding is stripped here so every card gets the
    same tidy output without touching every call site.
    """
    out = []
    for k, v in pairs:
        if v is None:
            continue
        out.append(f"• {str(k).strip()}: {v}")
    return out


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


def live_stages(phone: str, stages: list[tuple[str, str, float | None]],
                elapsed: float, note: str | None = None) -> str:
    """Checklist card shown from the moment Send is pressed.

    Before this existed the panel showed nothing for the first few minutes of a
    job -- and on this host just opening Chromium takes 150-200 seconds, so there
    was no way to tell a working bot from a stuck one.

    stages: [(label, state, seconds)] where state is done | active | pending |
    failed. `seconds` is how long a finished step took.
    """
    mark = {"done": "✅", "active": "⏳", "pending": "◻️", "failed": "⚠️"}
    rows = []
    for label, state, secs in stages:
        line = f"{mark.get(state, '◻️')} {label}"
        if state == "done" and secs:
            line += f"  ({secs:.0f}s)"
        elif state == "active":
            line += "  …"
        rows.append(line)
    lines = ["⚙️ WORKING — Live", DIVIDER, f"📱 {phone}", *rows,
             f"⏱ total {fmt_duration(elapsed)}"]
    if note:
        lines.append(note)
    lines.append(f"🕒 {now_hms()}")
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


def _ago(ts: float | None) -> str:
    """How long ago a timestamp was, in words."""
    if not ts:
        return "never"
    d = max(0, time.time() - float(ts))
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d / 60)}m ago"
    if d < 172800:
        return f"{int(d / 3600)}h ago"
    return f"{int(d / 86400)}d ago"


def panel_home(accounts: int, ready: int, active: str | None,
               engine: str | None = None, ping_ms: int | None = None,
               contacts: int | None = None, running: int = 0,
               content: str | None = None, last_run: dict | None = None) -> str:
    """Home screen: what can send, what is loaded, what happened last.

    Deliberately does NOT show "Bot: online" (if it were offline no card would
    arrive), a version string, or a progress bar. Every row here is something the
    owner can act on.
    """
    acc_txt = None
    if accounts:
        missing = max(0, accounts - max(0, ready))
        acc_txt = f"{accounts}"
        if ready:
            acc_txt += f" ({ready} ready"
            acc_txt += f" · {missing} without contacts)" if missing else ")"
        elif missing:
            acc_txt += f" ({missing} without contacts)"
    lr = last_run or {}
    lr_txt = None
    if lr:
        bits = [f"{int(lr.get('sent', 0)):,} sent"]
        if lr.get("failed"):
            bits.append(f"{int(lr['failed']):,} failed")
        if lr.get("skipped"):
            bits.append(f"{int(lr['skipped']):,} skipped")
        if lr.get("elapsed"):
            bits.append(fmt_duration(float(lr["elapsed"])))
        lr_txt = " · ".join(bits) + f" ({_ago(lr.get('at'))})"

    lines = [
        "🤖 EITAA MANAGER",
        DIVIDER,
        *_rows([
            ("Eitaa", _ping_mark(ping_ms)),
            ("Accounts", acc_txt or "none yet"),
            ("Contacts", f"{contacts:,} sendable" if contacts else "none saved yet"),
            ("Active", active or "—"),
            ("Job", f"⏳ {running} running" if running else "idle"),
            ("Content", content or "nothing set"),
            ("Last run", lr_txt),
            ("Engine", engine),
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
                "Logged in, but no contacts came back. Add contacts in Eitaa, then tap "
                "🔄 Update Contacts on this account."),
    )


def account_panel(account: str, phone: str, contacts: int | None, pvs: int | None,
                  engine: str | None, busy: bool, peers: int | None = None,
                  saved: int | None = None, saved_age: float | None = None,
                  meta_age: float | None = None, pending: int | None = None) -> str:
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
        footer = ("No contacts saved for this account. Tap 🔄 Update Contacts to read "
                  "them from Eitaa (takes seconds).")
    else:
        footer = f"Ready to send to {saved:,} saved contact(s)."

    # "On Eitaa" is a snapshot from the last measurement, so it says WHEN it was
    # taken. Without that this row silently disagreed with reality (it showed
    # 1,414 for an account that had 1,094) and nothing explained why.
    on_eitaa = None
    if isinstance(contacts, int) and contacts >= 0:
        on_eitaa = f"{contacts:,}"
        if meta_age:
            on_eitaa += f" (measured {_ago(time.time() - meta_age * 3600)})"

    return card(
        "👤 ACCOUNT",
        [
            ("Phone", phone),
            ("State", "⏳ busy" if busy else "🟢 idle"),
            ("Saved", f"{saved:,} contacts ({age})" if saved else "none"),
            ("On Eitaa", on_eitaa or "—"),
            ("Chats", pvs if isinstance(pvs, int) and pvs >= 0 else None),
            ("Already sent", f"{pending:,} got the current content" if pending else None),
            # Only meaningful while the browser-free engine is enabled.
            ("Peers", peers if (peers and engine == "direct") else None),
        ],
        footer=footer,
    )


def contacts_saved(phone: str, count: int, with_peer: int, elapsed: float,
                   replaced: int | None = None, partial: bool = False) -> str:
    """Result of caching an account's contacts list."""
    pairs = [
        ("Phone     ", phone),
        ("Saved     ", f"{count:,} contacts"),
        ("With id   ", f"{with_peer:,}"),
        ("Time      ", fmt_duration(elapsed)),
    ]
    if replaced is not None and replaced != count:
        pairs.insert(2, ("Previously", f"{replaced:,}"))
    if partial:
        title = "🛑 CONTACTS SAVED (stopped early)"
        footer = ("Stopped before the list finished, so this is only part of it. "
                  "Run 🔄 Update Contacts again to complete it.")
    elif count:
        title = "📥 CONTACTS SAVED"
        footer = "Sends from this account now start immediately."
    else:
        title = "📥 NO CONTACTS FOUND"
        footer = ("The contacts list came back empty. Open Eitaa's Contacts view "
                  "once, then try again.")
    return card(title, pairs, footer=footer)


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


def restriction_card(account: str, reason: str, sent_before: int,
                     paused: bool = True) -> str:
    """The server refused a recipient (PEER_FLOOD, spam warning, ...).

    `paused=False` is used when the owner turned off "Pause on limit": the card
    is still posted so the restriction is never hidden, but the run continues and
    only Stop ends it.
    """
    peer_flood = "PEER_FLOOD" in str(reason).upper()
    footer = None
    if peer_flood:
        footer = ("PEER_FLOOD is not a timed wait: Eitaa is refusing messages from "
                  "this account to people it is not in two-way contact with. Waiting "
                  "does not clear it — it usually needs a few quiet days, and it hits "
                  "new accounts fastest.")
    if not paused:
        footer = ((footer + " ") if footer else "") + \
                 "Pause on limit is OFF, so the run continues. Use Stop to end it."
    return card(
        "🚫 LIMIT DETECTED",
        [
            ("Account    ", account),
            ("Reason     ", sanitize(reason, 200)),
            ("Sent before", sent_before),
            ("Action     ", "job auto-paused" if paused
                            else "continuing (pause on limit is off)"),
            ("Time       ", now_hms()),
        ],
        footer=footer,
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
                     "contacts saved. Open each one and tap '🔄 Update Contacts', then "
                     "send again.")
    lines.append(f"🕒 {now_hms()}")
    return "\n".join(lines)




