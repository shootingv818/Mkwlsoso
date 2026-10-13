"""Cards for the photo export, in the owner's requested shape.

The layout is the one the owner asked for:

    | (gear) - #photos
    -------------------------------
    --| Phone - 989...
    * Name : ...
    * Status : SCANNING
    ...

    * Overall
    [##########] 42%

    * Scan Chats
    [#####-----] 250 / 601
    -------------------------------
    --| (globe) - Worker : #W3_412

`bot.cards.card()` already produces the header/DIVIDER/footer frame and its
`body` argument is multi-line aware, so the whole card is built through it
instead of hand-rolling a second card format.
"""

from __future__ import annotations

import time

from bot import cards

# The bar the owner used: light shade for empty, dark shade for filled.
_EMPTY = "\u2591"   # light shade
_FULL = "\u2588"    # full block
_WIDTH = 10


def bar(done: int, total: int, width: int = _WIDTH) -> str:
    """`[####------]` sized to `width`. Never full until it really is."""
    if total <= 0:
        return "[" + _EMPTY * width + "]"
    ratio = min(1.0, max(0.0, done / total))
    filled = int(round(ratio * width))
    if filled >= width and done < total:
        filled = width - 1
    return "[" + _FULL * filled + _EMPTY * (width - filled) + "]"


def pct_bar(done: int, total: int, width: int = _WIDTH) -> str:
    pct = 0 if total <= 0 else int(min(100, max(0, done * 100 // total)))
    return f"{bar(done, total, width)} {pct}%"


def count_bar(done: int, total: int, width: int = _WIDTH) -> str:
    return f"{bar(done, total, width)} {done:,} / {total:,}"


def worker_tag(account: str) -> str:
    """A stable short worker id per account, so the footer has an identity."""
    digits = "".join(ch for ch in str(account) if ch.isdigit())
    tail = digits[-3:] if len(digits) >= 3 else (digits or "000")
    return f"#W{len(digits) % 10}_{tail}"


def progress(*, account: str, phone: str, direction: str, status: str,
             step: str, chats_total: int = 0, chats_scanned: int = 0,
             photos_found: int = 0, photos_target: int = 0,
             downloaded: int = 0, pages_built: int = 0, pages_total: int = 0,
             files_sent: int = 0, files_total: int = 0,
             elapsed: float = 0.0, note: str | None = None,
             pace: str | None = None) -> str:
    """The live card. Every stage keeps its own bar so nothing looks stuck."""
    dir_label = {"sent": "sent by me", "received": "received",
                 "both": "sent + received"}.get(direction, direction)

    # Overall weights the stages by how long each was measured to take.
    weights = (("scan", 0.30), ("download", 0.25), ("pdf", 0.35), ("send", 0.10))
    frac = {
        "scan": (chats_scanned / chats_total) if chats_total else 0.0,
        "download": (downloaded / photos_target) if photos_target else 0.0,
        "pdf": (pages_built / pages_total) if pages_total else 0.0,
        "send": (files_sent / files_total) if files_total else 0.0,
    }
    overall = sum(min(1.0, frac[k]) * w for k, w in weights)

    lines = [
        f"--| Phone - {phone}",
        f"\u2022 Mode : {dir_label}",
        f"\u2022 Status : {status}",
        f"\u2022 Step : {step}",
        f"\u2022 Chats : {chats_scanned:,} / {chats_total:,}",
        f"\u2022 Photos : {photos_found:,}",
        f"\u2022 Elapsed : {cards.fmt_duration(elapsed)}",
    ]
    # A paced run can sit still for seconds at a time; an ETA is what tells the
    # owner it is working rather than wedged.
    if photos_target and downloaded:
        lines.append(f"\u2022 Remaining : ~"
                     f"{cards.eta(downloaded, photos_target, elapsed)}")
    elif chats_total and chats_scanned:
        lines.append(f"\u2022 Remaining : ~"
                     f"{cards.eta(chats_scanned, chats_total, elapsed)}")
    if pace:
        lines.append(f"\u2022 Pace : {pace}")
    lines += [
        "",
        "\u2022 Overall",
        pct_bar(int(overall * 1000), 1000),
        "",
        "\u2022 Scan Chats",
        count_bar(chats_scanned, chats_total),
        "",
        "\u2022 Download Photos",
        count_bar(downloaded, photos_target),
        "",
        "\u2022 Build PDF",
        (count_bar(pages_built, pages_total) if pages_total
         else f"{bar(0, 0)} WAITING"),
        "",
        "\u2022 Send Files",
        (count_bar(files_sent, files_total) if files_total
         else f"{bar(0, 0)} WAITING"),
    ]
    if note:
        lines += ["", f"\u2022 Note : {cards.sanitize(note, 160)}"]

    return cards.card(
        "| \u2699 - #photos",
        body="\n".join(lines),
        footer=f"--| \U0001f30d - Worker : {worker_tag(account)}",
    )


def finished(*, account: str, phone: str, direction: str, photos: int,
             sent_by_me: int, received: int, chats_with_photos: int,
             chats_total: int, files: list[dict], elapsed: float,
             skipped: int = 0, stopped: bool = False,
             partial: bool = False, requested: int = 0,
             photos_available: int = 0, rate_limited: bool = False,
             waited: int = 0, note: str | None = None,
             top_chats: list[tuple[str, int]] | None = None) -> str:
    """The result card.

    A run the server cut short must NOT say DONE with a full bar. The first
    version did exactly that -- 15 photos of 500 under a green tick -- so the
    status, the bar and the footer all follow what actually happened.
    """
    total_kb = sum(int(f.get("kb") or 0) for f in files)
    asked = requested or photos
    if stopped:
        status, title = "STOPPED", "| \U0001f6d1 - #photos"
    elif partial:
        status, title = "PARTIAL", "| \u26a0\ufe0f - #photos"
    else:
        status, title = "DONE", "| \u2705 - #photos"

    lines = [
        f"--| Phone - {phone}",
        f"\u2022 Mode : {direction}",
        f"\u2022 Status : {status}",
        f"\u2022 Chats scanned : {chats_total:,}",
        f"\u2022 Chats with photos : {chats_with_photos:,}",
    ]
    if photos_available and photos_available != photos:
        lines.append(f"\u2022 Photos in chats : {photos_available:,}")
    if asked and asked != photos:
        lines.append(f"\u2022 Photos requested : {asked:,}")
    lines += [
        f"\u2022 Photos exported : {photos:,}",
        f"\u2022 Sent by me : {sent_by_me:,}",
        f"\u2022 Received : {received:,}",
    ]
    if skipped:
        lines.append(f"\u2022 Not downloaded : {skipped:,}")
    if waited:
        lines.append(f"\u2022 Waited for limits : {waited}s")
    lines += [
        f"\u2022 Files : {len(files)}  ({total_kb:,} KB)",
        f"\u2022 Took : {cards.fmt_duration(elapsed)}",
        "",
        "\u2022 Exported",
        # Against what was ASKED for, so a short run reads short.
        count_bar(photos, asked or photos),
    ]
    # Name the chats the photos came from. Only private chats are ever scanned,
    # and this is how the owner can SEE that rather than take it on trust: a
    # channel or group name appearing here would mean the filter had broken.
    if top_chats:
        lines += ["", "\u2022 Top chats (private only)"]
        for name, n in top_chats[:10]:
            lines.append(f"\u2022   {cards.sanitize(name, 28) or '(no name)'}"
                         f" - {n:,}")
    lines.append("")
    for i, f in enumerate(files, start=1):
        lines.append(f"\u2022 {i}. {f.get('name')} - {f.get('pages')} page(s), "
                     f"{f.get('kb')} KB")
    if note:
        lines += ["", f"\u2022 Note : {cards.sanitize(note, 200)}"]

    if stopped:
        footer = "Stopped early -- what had been collected was still exported."
    elif rate_limited and partial:
        # Do NOT promise that pressing again continues where this stopped. There
        # is no memory of what was already exported yet, so a second run picks the
        # same newest photos. Say what actually helps instead.
        footer = ("Eitaa rate-limited this account. Raise MKWL_PHOTO_DELAY to be "
                  "gentler, or narrow the run with MKWL_PHOTO_MAX -- a second run "
                  "right now would fetch the same newest photos again, not the "
                  "missing ones.")
    elif partial:
        footer = ("Some photos could not be downloaded; the rest are in the "
                  "file(s) above, one photo per page.")
    else:
        footer = "Each photo is on its own page. The files were sent to this chat."
    return cards.card(title, body="\n".join(lines), footer=footer)


def nothing_found(*, account: str, phone: str, direction: str,
                  chats_total: int, elapsed: float) -> str:
    return cards.card(
        "| \u2699 - #photos",
        body="\n".join([
            f"--| Phone - {phone}",
            f"\u2022 Mode : {direction}",
            "\u2022 Status : NOTHING FOUND",
            f"\u2022 Chats scanned : {chats_total:,}",
            f"\u2022 Took : {cards.fmt_duration(elapsed)}",
        ]),
        footer=("No photo matched this filter. Try 'sent + received', or widen "
                "the date range if one was set."),
    )


def started(*, account: str, phone: str, direction: str) -> str:
    return cards.card(
        "| \u2699 - #photos",
        body="\n".join([
            f"--| Phone - {phone}",
            f"\u2022 Mode : {direction}",
            "\u2022 Status : STARTING",
            "\u2022 Step : PREPARING",
            f"\u2022 Time : {cards.now_hms()}",
        ]),
        footer=("Reading this account's chats for photos. A live card follows. "
                "Nothing is sent to anybody on Eitaa."),
    )
