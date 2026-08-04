"""Cards for Contact Boost, in the owner's requested shape.

    | (gear) - #boost
    -------------------------------
    --| Phone - 989...
    * Status : RUNNING
    ...
    * Probe Numbers
    [#####-----] 200 / 400
    -------------------------------
    --| (globe) - Worker : #W2_238

The bars and the worker tag are the ones photo_export already uses, so the two
features look like the same bot rather than two bolted-together halves.
"""

from __future__ import annotations

from bot import cards
from photo_export.cards import bar, count_bar, pct_bar, worker_tag

__all__ = ["bar", "count_bar", "pct_bar", "worker_tag",
           "progress", "finished", "skipped"]


def _hit_rate(hits: int, tried: int) -> str:
    if tried <= 0:
        return "--"
    return f"{hits * 100 / tried:.0f}%"


def _pick_line(random_pick: bool, span) -> str | None:
    """Where in the prefix the numbers came from.

    With a random pick the numbers are scattered, so a "block" would be a lie;
    the honest description is the span they were drawn from.
    """
    lo, hi = (span or ("", ""))
    if not lo or not hi:
        return None
    if random_pick:
        return f"\u2022 Picked : at random between {lo} and {hi}"
    return f"\u2022 Block : {lo} \u2192 {hi}"


def progress(*, account: str, phone: str, prefix: str, status: str, step: str,
             probe_total: int, probed: int, matched: int,
             contacts_before: int | None = None, contacts_now: int | None = None,
             phone_format: str | None = None, waited: int = 0,
             random_pick: bool = True, span=("", ""),
             elapsed: float = 0.0, note: str | None = None) -> str:
    """The live card while numbers are being probed."""
    lines = [
        f"--| Phone - {phone}",
        f"\u2022 Status : {status}",
        f"\u2022 Step : {step}",
        f"\u2022 Prefix : {prefix or '--'}",
    ]
    pick = _pick_line(random_pick, span)
    if pick:
        lines.append(pick)
    lines += [
        f"\u2022 Probing : {probe_total:,} numbers",
        f"\u2022 Matched : {matched:,}",
        f"\u2022 Hit rate : {_hit_rate(matched, probed)}",
    ]
    if contacts_before is not None:
        lines.append(f"\u2022 Contacts before : {contacts_before:,}")
    if contacts_now is not None:
        lines.append(f"\u2022 Contacts now : {contacts_now:,}")
    if phone_format:
        lines.append(f"\u2022 Format : {phone_format}")
    if waited:
        lines.append(f"\u2022 Waited for limits : {waited}s")
    lines += [
        f"\u2022 Elapsed : {cards.fmt_duration(elapsed)}",
        "",
        "\u2022 Overall",
        pct_bar(probed, probe_total),
        "",
        "\u2022 Probe Numbers",
        count_bar(probed, probe_total),
        "",
        "\u2022 Add Contacts",
        # Against the numbers PROBED, not against `matched` itself -- a bar that
        # fills up as soon as one number matches would suggest the run had found
        # everything there was to find.
        f"{bar(matched, probe_total)} {matched:,} added",
    ]
    if note:
        lines += ["", f"\u2022 Note : {cards.sanitize(note, 160)}"]
    return cards.card(
        "| \u2699 - #boost",
        body="\n".join(lines),
        footer=f"--| \U0001f30d - Worker : {worker_tag(account)}",
    )


def finished(*, account: str, phone: str, prefix: str, probe_total: int,
             probed: int, matched: int, contacts_before: int,
             contacts_after: int, elapsed: float,
             span=("", ""), random_pick: bool = True,
             shared_range: bool = True, accounts_served: int = 0,
             phone_format: str | None = None, waited: int = 0,
             pool: int = 0, pool_skipped=(), returned: int = 0, capacity: int = 0,
             used_under_prefix: int = 0, left_under_prefix: int = 0,
             lifetime_tried: int = 0, lifetime_hits: int = 0,
             peers_new: int = 0, peers_total: int = 0,
             stopped: bool = False, rate_limited: bool = False,
             note: str | None = None) -> str:
    """The result card. `Increase` is MEASURED, not the server's own count.

    `contacts.importContacts` reports a number in `imported` whenever it belongs
    to a real user -- including when that user is already a contact -- so
    `matched` answers "how many of these numbers exist" and only
    `contacts_after - contacts_before` answers "how many contacts did I gain".
    Both are shown, because the gap between them is the useful signal.
    """
    increase = max(0, contacts_after - contacts_before)
    if stopped:
        status, title = "STOPPED", "| \U0001f6d1 - #boost"
    elif rate_limited and probed < probe_total:
        status, title = "PARTIAL", "| \u26a0\ufe0f - #boost"
    elif matched == 0:
        status, title = "NOBODY FOUND", "| \u26a0\ufe0f - #boost"
    else:
        status, title = "DONE", "| \u2705 - #boost"

    lines = [
        f"--| Phone - {phone}",
        f"\u2022 Status : {status}",
        f"\u2022 Prefix : {prefix or '--'}"
        + (f"  ({capacity:,} numbers)" if capacity else "")
        + (f"  \u2014 picked at random from {pool}" if pool > 1 else ""),
    ]
    if pool_skipped:
        lines.append(f"\u2022 Skipped : {', '.join(pool_skipped)} "
                     f"(sampled, found nobody)")
    pick = _pick_line(random_pick, span)
    if pick:
        lines.append(pick)
    lines += [
        f"\u2022 Numbers probed : {probed:,}" +
        (f" of {probe_total:,}" if probed != probe_total else ""),
        f"\u2022 Matched on Eitaa : {matched:,}",
        f"\u2022 Hit rate : {_hit_rate(matched, probed)}",
        "",
        f"\u2022 Contacts before : {contacts_before:,}",
        f"\u2022 Contacts after : {contacts_after:,}",
        f"\u2022 Increase : +{increase:,}",
    ]
    # Matched but did not increase = those numbers were already contacts. Say so
    # rather than leaving two numbers that disagree.
    if matched > increase:
        lines.append(f"\u2022 Already had : {matched - increase:,}")
    # One line instead of a "PEERS SAVED" card per batch.
    if peers_new:
        lines.append(f"\u2022 Fast-send ready : +{peers_new:,}"
                     + (f"  ({peers_total:,} total)" if peers_total else ""))
    if waited:
        lines.append(f"\u2022 Waited for limits : {waited}s")
    if returned:
        lines.append(f"\u2022 Returned unused : {returned:,} numbers")
    lines += [
        f"\u2022 Took : {cards.fmt_duration(elapsed)}",
        "",
        "\u2022 Probed",
        count_bar(probed, probe_total),
        "",
        "\u2022 Added",
        # Against the block that was probed, so a small yield READS small
        # instead of hiding behind a full bar.
        f"{count_bar(increase, probed or probe_total)} contacts",
    ]
    if phone_format:
        lines.append("")
        lines.append(f"\u2022 Format : {phone_format} (remembered, "
                     f"next run skips the probe)")
    if capacity:
        lines.append(f"\u2022 Used under prefix : {used_under_prefix:,} of "
                     f"{capacity:,}  ({left_under_prefix:,} left)")
    if shared_range and accounts_served > 1:
        lines.append(f"\u2022 Accounts served : {accounts_served} "
                     f"(no two get the same number)")
    if lifetime_tried:
        lines.append(f"\u2022 This account : {lifetime_hits:,} found in "
                     f"{lifetime_tried:,} probed "
                     f"({_hit_rate(lifetime_hits, lifetime_tried)})")
    if note:
        lines += ["", f"\u2022 Note : {cards.sanitize(note, 200)}"]

    if stopped:
        footer = ("Stopped early. Only the numbers actually submitted were used "
                  "up; the rest went back for the next run.")
    elif rate_limited:
        footer = ("Eitaa rate-limited this account, so the run ended early. The "
                  "numbers it never submitted went back, so pressing Boost "
                  "Contacts again later loses nothing.")
    elif matched == 0:
        footer = ("None of those numbers is on Eitaa. They are marked as used, so "
                  "pressing again picks a completely different set.")
    elif random_pick:
        footer = ("Numbers are picked at RANDOM from across the prefix and never "
                  "handed out twice, so no two accounts share a contact and the "
                  "list does not look machine-generated. 'Increase' is the real "
                  "contact count before vs after, not the server's own tally.")
    else:
        footer = ("Sequential mode. Every number is remembered so it is never "
                  "used twice. 'Increase' is the real contact count before vs "
                  "after, not the server's own tally.")
    return cards.card(title, body="\n".join(lines), footer=footer)


def skipped(*, account: str, phone: str, reason: str) -> str:
    """Boost was on but could not run (no prefix, prefix exhausted, ...)."""
    return cards.card(
        "| \u26a0\ufe0f - #boost",
        body="\n".join([
            f"--| Phone - {phone}",
            "\u2022 Status : SKIPPED",
            f"\u2022 Reason : {cards.sanitize(reason, 160)}",
        ]),
        footer=("Set the prefix in Settings \u2192 'Boost Prefix', or turn "
                "Contact Boost off there."),
    )
