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


# ---- diagnostics shared by the cards that close or interrupt a run ----
#
# These exist because the cards used to answer "how many went out" but not the
# question you actually ask when a run ends unexpectedly: did the loop REACH THE
# END of the list, or did it give up part way? Everything below is derived from
# numbers the cards already receive, so no caller has to change.

def _n(v: object) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def coverage(sent: object, failed: object, total: object) -> tuple[int, int]:
    """(attempted, never_tried) for a run. `skipped` is excluded on purpose:
    skipped recipients were filtered out BEFORE the loop, so they are not part of
    what the loop was supposed to walk."""
    attempted = _n(sent) + _n(failed)
    return attempted, max(0, _n(total) - attempted)


def pace(attempted: object, elapsed: object) -> str | None:
    """`1.9s each · 32/min` -- the number needed to judge whether a run was
    slow or just long."""
    a, el = _n(attempted), float(elapsed or 0)
    if a <= 0 or el <= 0:
        return None
    return f"{el / a:.1f}s each · {a / el * 60:.0f}/min"


def debug_line(**kv: object) -> str:
    """One compact copy-pasteable line. When something goes wrong the whole state
    of the run can be quoted in a bug report without screenshotting five cards."""
    parts = [f"{k}={v}" for k, v in kv.items() if v is not None and v != ""]
    return "🔍 " + " ".join(parts)


# Server restriction strings, classified. The scope is the part that matters:
# a per-recipient refusal means "skip this person", an account-wide one means
# "this account is done for now" -- and they must not be treated the same.
def limit_kind(reason: object) -> dict:
    up = str(reason or "").upper()
    m = re.search(r"FLOOD_WAIT_(\d+)", up)
    if m:
        secs = int(m.group(1))
        return {"key": "flood_wait", "scope": "timed wait", "wait": secs,
                "label": f"timed wait, {secs}s",
                "note": (f"A timed wait: the server asked for {secs}s and it clears "
                         f"on its own. This is NOT a permanent refusal and the "
                         f"recipient is not added to the refused list.")}
    if "ALL_PEER_FLOOD" in up:
        return {"key": "all_peer_flood", "scope": "whole account", "wait": None,
                "label": "account-wide",
                "note": ("ALL_PEER_FLOOD is account-wide, not about one recipient: "
                         "Eitaa has stopped this ACCOUNT from messaging people it "
                         "is not in two-way contact with. Every recipient tried "
                         "from here on will refuse for the same reason, so the "
                         "refused list is being filled with people who did nothing "
                         "wrong. It eases after quiet time and hits new accounts "
                         "fastest.")}
    if "PEER_FLOOD" in up:
        return {"key": "peer_flood", "scope": "this recipient", "wait": None,
                "label": "per-recipient",
                "note": ("PEER_FLOOD is not a timed wait: Eitaa is refusing messages "
                         "from this account to people it is not in two-way contact "
                         "with. Waiting does not clear it — it usually needs a few "
                         "quiet days, and it hits new accounts fastest.")}
    if "SLOWMODE" in up:
        return {"key": "slowmode", "scope": "this chat", "wait": None,
                "label": "slow mode",
                "note": ("Slow mode is set on that chat: it caps how often anyone "
                         "may post there. It is not an account restriction.")}
    if "SPAM" in up:
        return {"key": "spam", "scope": "whole account", "wait": None,
                "label": "spam warning",
                "note": ("A spam warning is aimed at the ACCOUNT. Continuing to send "
                         "while it stands is what turns it into a longer block.")}
    if "TOO_MANY" in up:
        return {"key": "too_many", "scope": "whole account", "wait": None,
                "label": "too many requests",
                "note": "Too many requests from this account. Slow the run down."}
    if "FLOOD" in up:
        return {"key": "flood", "scope": "whole account", "wait": None,
                "label": "flood",
                "note": "A flood limit with no stated wait; treat it as account-wide."}
    return {"key": "unknown", "scope": "unknown", "wait": None, "label": "unclassified",
            "note": ("This code is not one the panel recognises. It was treated as a "
                     "limit because the text matched a limit pattern -- quote this "
                     "card if it looks like a false positive.")}


# code -> the first thing worth doing about it
_CODE_HINTS = {
    "not_logged_in": "Run 🔎 Check Session, then log in again from ➕ Add Account.",
    "no_recipients": "Nothing was left to target: check Skipped / Refused above, "
                     "then ➕ Build Contacts or 🧯 reset the refused list.",
    "peer_id_invalid": "The saved peer is stale. Run 🔄 Update Contacts to re-resolve it.",
    "ui_timeout": "The page did not answer in time -- almost always the server "
                  "(CPU steal / swap), not Eitaa.",
    "locate_failed": "The upload could not be found in the page again; the file "
                     "state was rebuilt and lost.",
    "timeouterror": "Something exceeded its time budget. Compare with ⏱ RUN TIMING.",
    "modulenotfounderror": "Code is missing on this server -- the deploy is "
                           "incomplete or on the wrong branch.",
}


def code_hint(code: object) -> str | None:
    key = str(code or "").strip().lower()
    if not key:
        return None
    if key in _CODE_HINTS:
        return _CODE_HINTS[key]
    for k, v in _CODE_HINTS.items():
        if k in key:
            return v
    return None


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


#: How many finished accounts keep a detailed two-line block on the live card.
#: Telegram caps a message near 4096 characters, and a 50-account run would blow
#: through that; the rest are summarised in one tally row.
_MULTI_DETAIL_ROWS = 6


def live_send_multi(accounts: list[dict], current: str | None, sent: int,
                    failed: int, total: int, elapsed: float,
                    status: str = "🟢 Sending", engine: str | None = None,
                    kind: str | None = None, parallel: int = 1) -> str:
    """ONE live card for a simultaneous multi-account send.

    `sent`/`failed`/`total` are the COMBINED numbers across every selected
    account (their contact lists added together), so the top block answers "how
    much of the whole job is done". `current` names the account that most
    recently sent, and the breakdown block shows each account's own progress.

    accounts: [{"phone": str, "sent": int, "failed": int, "total": int,
                "state": "pending|running|done|stopped|failed|limited"}]
    """
    running = [a for a in accounts
               if str(a.get("state")) in ("running", "preparing")]
    now_label = ", ".join(str(a.get("phone")) for a in running) or (current or "—")
    lines = [
        "🚀 MULTI SEND — Live",
        DIVIDER,
        bar(sent + failed, total),
        *_rows([
            ("Status  ", status),
            ("Type    ", kind),
            ("Mode    ", (f"parallel · {parallel} at a time" if parallel > 1
                          else "one account at a time")),
            ("Accounts", _account_tally(accounts)),
            # With more than one account in flight this has to name them all;
            # a single "Now" field was only ever true for a sequential run.
            ("Now     ", now_label),
            ("Sent    ", f"{sent} of {total}"),
            ("Failed  ", failed or None),
            ("Elapsed ", fmt_duration(elapsed)),
            ("Left    ", eta(sent + failed, total, elapsed)),
        ]),
    ]
    if accounts:
        lines.append(DIVIDER)
        # A bar per account: the eye reads a bar far faster than "120/500", and
        # with several running at once the numbers alone were unreadable.
        # Positions are NOT printed any more -- in a parallel run they would
        # imply an execution order that does not exist.
        active = [a for a in accounts
                  if str(a.get("state")) in ("running", "preparing")]
        finished = [a for a in accounts
                    if str(a.get("state")) in ("done", "failed", "stopped",
                                               "limited", "no_targets")]
        waiting = [a for a in accounts if a not in active and a not in finished]
        # Telegram caps a message at ~4096 chars, so only a window is detailed:
        # everything in flight, then the most recent finishers, then a tally.
        shown = active + finished[-_MULTI_DETAIL_ROWS:]
        for a in shown:
            state = str(a.get("state", "pending"))
            mark = _STATE_MARK.get(state, "•")
            done = int(a.get("sent", 0) or 0)
            tot = int(a.get("total", 0) or 0)
            head = f"{mark} {a.get('phone', '')}"
            if a.get("failed"):
                head += f" · ✗{a['failed']}"
            if state == "limited":
                head += " · limited"
            lines.append(head)
            lines.append(f"   {bar(done, tot, width=10)}  {done:,}/{tot:,}")
        hidden = len(finished) - len(finished[-_MULTI_DETAIL_ROWS:])
        tail = []
        if hidden > 0:
            tail.append(f"{hidden} more finished")
        if waiting:
            tail.append(f"{len(waiting)} waiting")
        if tail:
            lines.append("• " + " · ".join(tail))
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
                  engine: str | None = None, saved: int | None = None,
                  tiers: dict | None = None) -> str:
    """The card posted after a successful login.

    `tiers` is the optional Send Order summary ({online, today, recently}). It is
    None whenever that feature is off, or whenever the status data could not
    support the counts -- and then these rows are simply absent. They are never
    shown as zeros, because 0/0/0 reads as "nobody is active" while the truth in
    that case is "we could not tell", and those are different statements.
    """
    rows: list[tuple[str, object]] = [
        ("Phone   ", phone),
        ("Contacts", f"{contacts:,}" if isinstance(contacts, int) and contacts >= 0 else "—"),
        ("Chats   ", pvs if isinstance(pvs, int) and pvs >= 0 else "—"),
        ("Saved   ", f"{saved:,}" if saved else None),
    ]
    if tiers:
        total = tiers.get("total") or 0
        def _row(n):
            n = int(n or 0)
            return f"{n:,}" + (f"  ({n * 100.0 / total:.1f}%)" if total else "")
        rows += [
            ("Online  ", _row(tiers.get("online"))),
            ("Today   ", _row(tiers.get("today"))),
            ("Recently", _row(tiers.get("recently"))),
        ]
    rows.append(("Time    ", now_hms()))
    return card(
        "✅ ACCOUNT ADDED",
        rows,
        footer=("Contacts saved — this account is ready to send." if saved else
                "Logged in, but no contacts came back. Add contacts in Eitaa, then tap "
                "🔄 Update Contacts on this account."),
    )


def account_panel(account: str, phone: str, contacts: int | None, pvs: int | None,
                  engine: str | None, busy: bool, peers: int | None = None,
                  saved: int | None = None, saved_age: float | None = None,
                  meta_age: float | None = None, pending: int | None = None,
                  refused: int | None = None,
                  engine_ready: bool | None = None) -> str:
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
            # What a send would ACTUALLY reach: saved minus the ones Eitaa keeps
            # refusing from this account.
            ("Reachable", f"{max(0, (saved or 0) - refused):,} of {saved:,}"
                          if (saved and refused) else None),
            ("Refused", f"{refused:,} (Eitaa won't deliver to them)" if refused else None),
            ("On Eitaa", on_eitaa or "—"),
            ("Chats", pvs if isinstance(pvs, int) and pvs >= 0 else None),
            ("Already sent", f"{pending:,} got the current content" if pending else None),
            ("Engine", ("browser-free ready" if engine_ready
                        else "browser-free not captured yet")
                       if engine_ready is not None else None),
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
    """Mid-run progress. Carries a bar, pace and ETA so a run that is merely slow
    can be told apart from a run that has stalled -- the old card showed four
    counters with no sense of movement."""
    sent, failed, left = _n(sent), _n(failed), _n(left)
    attempted = sent + failed
    total = attempted + left
    return card(
        "📈 PROGRESS",
        [
            ("Progress", bar(attempted, total) if total else None),
            ("Sent    ", f"{sent:,}"),
            ("Failed  ", f"{failed:,}  ({_pct(failed, attempted)} of tried)"
                         if failed else "0"),
            ("Skipped ", f"{_n(skipped):,}" if skipped else None),
            ("Left    ", f"{left:,}"),
            ("Pace    ", pace(attempted, elapsed)),
            ("Left ~  ", eta(attempted, total, elapsed) if left else None),
            ("Elapsed ", fmt_duration(elapsed)),
        ],
    )


def send_finished(account: str, kind: str, sent: int, failed: int, skipped: int,
                  total: int, elapsed: float, stopped: bool = False,
                  reason: str | None = None, engine: str | None = None,
                  limits: int | None = None, via_bridge: int | None = None,
                  via_fallback: int | None = None,
                  job_id: str | None = None) -> str:
    """The card that closes a run.

    The old card said "Sent 199 of 1232" and left the only question that matters
    unanswered: were the other 1033 TRIED AND REFUSED, or NEVER REACHED because
    the loop gave up? Those are completely different bugs and they looked
    identical. `Attempted` / `Never tried` / `Verdict` separate them, and the
    debug line at the bottom carries the whole run state for a bug report.

    Every extra argument is optional, so this stays a drop-in for existing calls.
    """
    sent, failed, skipped = _n(sent), _n(failed), _n(skipped)
    total, elapsed = _n(total), float(elapsed or 0)
    attempted, untouched = coverage(sent, failed, total)

    if stopped:
        title = "🛑 SEND STOPPED"
    elif failed and not sent:
        title = "⚠️ SEND FAILED"
    elif failed:
        title = "⚠️ SEND FINISHED (with failures)"
    else:
        title = "✅ SEND FINISHED"

    # The verdict is the line to read first: it says whether the list was walked.
    if untouched and stopped:
        verdict = (f"🛑 ended early — {untouched:,} of {total:,} were never tried")
    elif untouched:
        verdict = (f"⚠️ loop ended with {untouched:,} never tried "
                   f"(it did not stop, so this is unexpected)")
    elif total:
        verdict = f"✅ walked the whole list — all {total:,} were tried"
    else:
        verdict = "◻️ there was nothing to walk"

    lines = [
        title,
        DIVIDER,
        f"📱 {account}",
        bar(attempted, total),
        verdict,
        *_rows([
            ("Type     ", kind),
            ("Engine   ", engine),
            ("Delivered", f"✅ {sent:,} of {total:,}  ({_pct(sent, total)})"),
            ("Failed   ", (f"✗ {failed:,}  ({_pct(failed, attempted)} of tried)")
                          if failed else None),
            ("Tried    ", f"{attempted:,} of {total:,}"),
            ("Never tried", f"{untouched:,}  ← still waiting" if untouched else None),
            ("Skipped  ", (f"{skipped:,} (already delivered in an earlier run)")
                          if skipped else None),
            ("Refused  ", f"{_n(limits):,} hit a server limit" if limits else None),
            ("Path     ", (f"api {_n(via_bridge):,} · browser {_n(via_fallback):,}")
                          if (via_bridge is not None or via_fallback is not None)
                          else None),
            ("Stopped by", sanitize(reason, 160) if reason else None),
            ("Duration ", fmt_duration(elapsed)),
            ("Pace     ", pace(attempted, elapsed)),
            ("Left ~   ", (eta(attempted, total, elapsed)) if untouched else None),
        ]),
        debug_line(job=job_id, sent=sent, failed=failed, skip=skipped, total=total,
                   tried=attempted, untried=untouched, limits=limits,
                   el=f"{elapsed:.0f}s", eng=engine, kind=kind,
                   stop=1 if stopped else 0),
        f"🕒 {now_hms()}",
    ]
    if untouched:
        lines.append("Press Send again to resume: the delivered ones are skipped, "
                     "so nobody gets it twice.")
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
               phase: str | None = None, sent: int | None = None,
               total: int | None = None, attempt: str | None = None) -> str:
    """An error, plus the first thing worth doing about it.

    `Fix` is derived from the code so the card is actionable on its own instead of
    being a code you then have to come and ask about. `Trace`/`Where`/`Phase`
    together say exactly which part of which job produced it.
    """
    return card(
        "⚠️ ERROR",
        [
            ("Trace  ", trace_id),
            ("Where  ", where),
            ("Phase  ", phase),
            ("Engine ", engine),
            ("Account", account),
            ("Target ", sanitize(target, 60) if target else None),
            ("Attempt", attempt),
            ("Progress", (f"{_n(sent):,} of {_n(total):,} at the time")
                         if total is not None else None),
            ("Code   ", code),
            ("Detail ", sanitize(detail, 400) if detail else None),
            ("Fix    ", code_hint(code)),
            ("Time   ", now_hms()),
        ],
        footer=debug_line(trace=trace_id, where=where, phase=phase, code=code,
                          eng=engine, acc=account),
    )


def preflight_card(phone: str, engine: str, kind: str, total: int, skipped: int,
                   refused: int, concurrency: int, delay: float,
                   per_send: float | None, file_mb: float | None = None) -> str:
    """What this run is about to do, and how long it should take.

    Posted before the first message. Without it, a run that would take hours
    looked exactly like one that would take three minutes, and there was no
    moment left to press Stop and change a setting.

    The estimate uses the LAST run's measured per-message time when there is one,
    because a guessed constant was always wrong on this host:

        seconds = total / (concurrency / (per_send + delay))
    """
    per = per_send if (per_send and per_send > 0) else 2.0
    rate = max(0.01, concurrency / (per + max(0.0, delay)))
    eta_s = total / rate
    return card(
        "🚦 READY TO SEND",
        [
            ("Phone      ", phone),
            ("Engine     ", engine),
            ("Type       ", kind + (f" ({file_mb:.1f} MB)" if file_mb else "")),
            ("Recipients ", f"{total:,}"),
            ("Skipping   ", (f"{skipped:,} already delivered" if skipped else None)),
            ("Refused    ", (f"{refused:,} Eitaa won't accept" if refused else None)),
            ("Pace       ", f"{concurrency} at a time, {delay:g}s between batches"),
            ("Expected   ", f"~{fmt_duration(eta_s)} at {rate:.2f} msg/s"),
        ],
        footer=("Estimate based on the last run's measured speed."
                if per_send else
                "First run for this account, so the estimate assumes 2s per message."),
    )


def dry_run_card(phone: str, engine: str, kind: str, ok: bool, detail: str,
                 send_seconds: float, total_seconds: float) -> str:
    """Result of the one-message test send to the owner's own Saved Messages."""
    return card(
        "🧪 TEST SEND — OK" if ok else "🧪 TEST SEND — FAILED",
        [
            ("Phone     ", phone),
            ("Engine    ", engine),
            ("Type      ", kind),
            ("Result    ", "delivered to your Saved Messages" if ok
                           else sanitize(detail, 160)),
            ("Send took ", f"{send_seconds:.1f}s"),
            ("Whole test", fmt_duration(total_seconds)),
        ],
        footer=("Check it in your own Saved Messages: this is exactly what your "
                "contacts would receive. 'Send took' is a real measurement of one "
                "message with the current engine."
                if ok else
                "Nothing was sent to any contact. Fix this before starting a "
                "campaign - it would fail the same way for everyone."),
    )


def pool_card(status: dict) -> str:
    """Standby browser sessions: what is warm and what it saved."""
    accounts = status.get("accounts") or {}
    lines = []
    for acc, info in accounts.items():
        state = "in use" if info.get("leased") else f"standby, idle {info.get('idle')}s"
        lines.append(f"• {acc}: {state} · {info.get('uses')} use(s)")
    saved = int(status.get("saved_launches") or 0)
    return card(
        "🏠 BROWSER STANDBY",
        [
            ("Enabled  ", "yes" if status.get("enabled") else "no (a browser per job)"),
            ("Warm now ", f"{status.get('warm', 0)} of {status.get('max_open')} allowed"),
            ("Idle close", f"after {int(status.get('idle_ttl') or 0)}s"),
            ("Launches saved", saved or None),
            ("Opened   ", status.get("created") or None),
            ("Recycled ", status.get("recycled") or None),
            ("Evicted  ", status.get("evicted") or None),
            ("Discarded", status.get("discarded") or None),
        ],
        body="\n".join(lines) if lines else None,
        footer=(f"Each saved launch is ~2-3 minutes not spent starting Chromium on "
                f"this server ({saved} so far). Sessions are opened only when a job "
                f"asks, closed when idle, and recycled so a long-lived browser cannot "
                f"grow unchecked."),
    )


def timing_card(phone: str, engine: str, timing: dict, concurrency: int,
                limits: int = 0, fallbacks: int = 0) -> str:
    """Where a run's time actually went.

    Added because a run measured 6.2s per message while the transport itself
    answered in 1-2s, and nothing in the panel could say what the other 4s were.
    """
    total = float(timing.get("total") or 0) or 1.0

    def share(key: str) -> str | None:
        v = float(timing.get(key) or 0)
        if v <= 0:
            return None
        return f"{v:.0f}s ({v / total * 100:.0f}%)"

    # Name the biggest cost outright. Four percentages still needed a human to
    # compare them; the verdict says which knob is the one worth touching.
    buckets = {k: float(timing.get(k) or 0)
               for k in ("transport", "fallback", "pacing", "other")}
    top = max(buckets, key=lambda k: buckets[k]) if any(buckets.values()) else None
    verdicts = {
        "transport": "Eitaa itself was the slowest part — the settings are not the "
                     "bottleneck.",
        "fallback": "The browser slow path dominated: the in-page API was missing "
                    "most sends. Worth investigating before raising the pace.",
        "pacing": "Most of the run was your own Send Delay. Lowering it is the "
                  "single fastest win here.",
        "other": "Most of the time was neither Eitaa nor the settings — that points "
                 "at the server itself (CPU steal, swap, disk).",
    }
    verdict = None
    if top and buckets[top] / total >= 0.4:
        verdict = f"➡️ {verdicts[top]}"

    return card(
        "⏱ RUN TIMING",
        [
            ("Phone      ", phone),
            ("Engine     ", f"{engine} · {concurrency} at a time"),
            ("Total      ", fmt_duration(total)),
            ("Sending    ", share("transport")),
            ("Slow path  ", share("fallback")),
            ("Pacing wait", share("pacing")),
            ("Everything else", share("other")),
            ("Biggest cost", top),
            ("Per message", f"{timing.get('per_send')}s" if timing.get("per_send") else None),
            ("Rate       ", f"{timing.get('msg_per_s')} msg/s"),
            ("Refused    ", limits or None),
            ("Browser fallbacks", fallbacks or None),
        ],
        body=verdict,
        footer="'Sending' is time Eitaa itself took. 'Pacing wait' is your Send Delay "
               "setting. A big 'Everything else' means the server (CPU steal, swap) "
               "rather than Eitaa or the settings.",
    )


def restriction_card(account: str, reason: str, sent_before: int,
                     paused: bool = True) -> str:
    """The server refused a recipient (PEER_FLOOD, spam warning, ...).

    `paused=False` is used when the owner turned off "Pause on limit": the card
    is still posted so the restriction is never hidden, but the run continues and
    only Stop ends it.
    """
    kind = limit_kind(reason)
    footer = kind["note"]
    if not paused:
        footer = ((footer + " ") if footer else "") + \
                 "Pause on limit is OFF, so the run continues. Use Stop to end it."

    # What this restriction does to the refused list is the part that silently
    # costs contacts: an account-wide code gets recorded against each recipient,
    # so a growing refused list is expected and is NOT evidence those people
    # rejected anything.
    if kind["key"] == "flood_wait":
        effect = "not recorded (timed waits are retried)"
    elif kind["scope"] == "whole account":
        effect = ("recipient added to the refused list — but the cause is the "
                  "account, not them")
    elif kind["key"] in ("peer_flood",):
        effect = "recipient added to the refused list permanently"
    else:
        effect = None

    if paused:
        action = "job auto-paused"
    else:
        action = "continuing (pause on limit is off)"

    return card(
        "🚫 LIMIT DETECTED",
        [
            ("Account    ", account),
            ("Reason     ", sanitize(reason, 200)),
            ("Kind       ", kind["label"]),
            ("Applies to ", kind["scope"]),
            ("Server wait", f"{kind['wait']}s" if kind.get("wait") else None),
            ("Sent before", f"{_n(sent_before):,} in THIS run "
                            f"(earlier runs are not counted here)"),
            ("Refused list", effect),
            ("Action     ", action),
            ("Next       ", ("only Stop ends this run" if not paused
                             else "press Send again to resume when it eases")),
            ("Time       ", now_hms()),
        ],
        footer=footer,
    )


def paused_card(account: str, reason: str, sent_before: int,
                total: int | None = None, brake: str | None = None) -> str:
    """Job auto-paused by the safety brake (not necessarily a real limit).

    Spelled out because this card and 🚫 LIMIT DETECTED look alike but mean
    opposite things: this one is OUR OWN brake giving up, not Eitaa refusing.
    Mistaking the two sends you hunting a limit that never happened.
    """
    left = None
    if total is not None:
        left = max(0, _n(total) - _n(sent_before))
    return card(
        "⏸ SEND PAUSED",
        [
            ("Account    ", account),
            ("Reason     ", sanitize(reason, 200)),
            ("Raised by  ", brake or "a local safety brake, not the Eitaa server"),
            ("Sent before", f"{_n(sent_before):,} in THIS run"),
            ("Not tried  ", f"{left:,}" if left else None),
            ("Action     ", "auto-paused; the error cards above say what kept failing"),
            ("Next       ", "fix the cause, then press Send again to resume"),
            ("Time       ", now_hms()),
        ],
        footer="This is the panel's own brake: too many failures in a row, so it "
               "stopped rather than hammering a broken session. Eitaa did not "
               "necessarily limit anything — check the error cards for the real code.",
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
    attempted, untouched = coverage(sent, failed, total)
    if untouched and stopped:
        verdict = f"🛑 ended early — {untouched:,} of {total:,} were never tried"
    elif untouched:
        verdict = f"⚠️ {untouched:,} of {total:,} were never tried"
    elif total:
        verdict = f"✅ every one of {total:,} was tried"
    else:
        verdict = None

    lines = [
        title,
        DIVIDER,
        bar(attempted, total),
    ]
    if verdict:
        lines.append(verdict)
    lines.extend(_rows([
        ("Accounts", _account_tally(accounts)),
        ("Type    ", kind),
        ("Engine  ", engine),
        ("Delivered", f"✅ {sent:,} of {total:,}  ({_pct(sent, total)})"),
        ("Failed  ", f"✗ {failed:,}  ({_pct(failed, attempted)} of tried)"
                     if failed else None),
        ("Tried   ", f"{attempted:,} of {total:,}"),
        ("Never tried", f"{untouched:,}" if untouched else None),
        ("Time    ", fmt_duration(elapsed)),
        ("Pace    ", pace(attempted, elapsed)),
    ]))
    if accounts:
        lines.append(DIVIDER)
        for i, a in enumerate(accounts, start=1):
            state = str(a.get("state", "done"))
            mark = _STATE_MARK.get(state, "•")
            a_sent, a_total = _n(a.get("sent")), _n(a.get("total"))
            a_failed = _n(a.get("failed"))
            row = f"{i}. {mark} {a.get('phone', '')} · {a_sent}/{a_total}"
            if a_failed:
                row += f" · ✗{a_failed}"
            # Per account too: tried-but-refused and never-reached are different.
            a_left = max(0, a_total - a_sent - a_failed)
            if a_left:
                row += f" · {a_left} untried"
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




