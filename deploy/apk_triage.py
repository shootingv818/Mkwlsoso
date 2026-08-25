#!/usr/bin/env python3
"""APK send triage — "it used to work, now it doesn't": which link broke?

WHY THIS EXISTS, AND WHY IT IS NOT ANOTHER PROBE
------------------------------------------------
The apk question was ANSWERED in docs/APK_SEND_STATUS.md: Eitaa blocks a document
whose MIME is the real apk type (application/vnd.android.package-archive). It does
NOT filter the filename or the bytes. Uploading as application/octet-stream with
the real ".apk" name in documentAttributeFilename delivers, and that fix is wired
into both send paths behind the `MKWL_APK_OCTET` toggle.

The existing deploy/apk_*.py scripts were written to FIND that answer. They still
work, but they all assume the cause is unknown and each explores one corner. When
apk sending stops on a server where it previously worked, the useful question is
narrower: the chain from "the toggle" to "the MIME on the wire" to "what Eitaa
does with it" has five links, and exactly one of them is usually broken.

This walks those links IN ORDER, cheapest first, and stops at the first one that
is wrong -- so you get one answer and one fix, instead of a wall of output.

    STAGE 1  toggle       is MKWL_APK_OCTET actually on, in the running bot?
    STAGE 2  policy       does effective_mime() rewrite .apk -> octet? (offline)
    STAGE 3  wiring       do BOTH send paths still call it? (source check)
    STAGE 4  os mime      what does this server map .apk to?
    STAGE 5  live         upload identical bytes under several MIMEs to YOUR OWN
                          Saved Messages and see which Eitaa accepts today.

Stages 1-4 need nothing but the repo: no browser, no network, no account. Run them
first. Stage 5 is the only one that touches Eitaa, is opt-in (--live), and
delivers ONLY to your own Saved Messages.

STAGE 5 IS THE IMPORTANT ONE when 1-4 are clean, because it answers the question
the old documents cannot: **is octet-stream still accepted?** If Eitaa has
tightened its filter, the recorded fix is dead and the probe shows which MIME (if
any) gets through now. That is a different bug from "the toggle is off", and they
need different responses.

Read-only with respect to the project: it imports modules, reads source, and
writes only a temp file plus its own log. It changes no settings and no state.

    cd ~/Mkwlsoso && .venv/bin/python deploy/apk_triage.py
    cd ~/Mkwlsoso && DISPLAY=:99 .venv/bin/python deploy/apk_triage.py --live --account 989132531349
"""
from __future__ import annotations

import argparse
import importlib
import mimetypes
import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BLOCKED = "application/vnd.android.package-archive"
OCTET = "application/octet-stream"

_findings: list[tuple[str, str, str]] = []   # (severity, stage, message)


def _say(line: str = "") -> None:
    print(line, flush=True)


def _hdr(n: int, title: str) -> None:
    _say()
    _say(f"{'=' * 66}")
    _say(f"STAGE {n} — {title}")
    _say(f"{'=' * 66}")


def ok(stage: str, msg: str) -> None:
    _say(f"  OK    {msg}")


def bad(stage: str, msg: str, fix: str) -> None:
    _say(f"  BROKEN  {msg}")
    _say(f"          FIX: {fix}")
    _findings.append(("BROKEN", stage, f"{msg} -> {fix}"))


def warn(stage: str, msg: str, note: str = "") -> None:
    _say(f"  WARN  {msg}")
    if note:
        _say(f"        {note}")
    _findings.append(("WARN", stage, msg))


# --------------------------------------------------------------- stage 1

def stage1_toggle() -> bool:
    """Is the toggle on where it matters: the RUNNING bot's environment?

    Three places can disagree, and only the third one sends files:
      * .env on disk            -- what you edited
      * the persisted setting   -- what the Settings panel stored
      * the live env var        -- what apk_mode.enabled() reads on every send

    The panel writes the live env var when you toggle it, so a bot that has been
    restarted since reads .env instead. If .env never got the key, the toggle
    silently reverts to OFF on every restart -- which looks exactly like
    "apk sending broke by itself".
    """
    _hdr(1, "the toggle: is APK mode actually on?")
    good = True

    # .env on disk
    env_path = os.path.join(_ROOT, ".env")
    env_val = None
    env_line = None
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8", errors="replace") as f:
            for i, raw in enumerate(f.read().splitlines(), 1):
                s = raw.strip()
                if s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                if k.strip() == "MKWL_APK_OCTET" and env_val is None:
                    # FIRST occurrence wins: config.py's loader uses setdefault.
                    env_val, env_line = v.strip().split("#")[0].strip(), i
        _say(f"  .env               : MKWL_APK_OCTET={env_val!r}"
             + (f"  (line {env_line})" if env_line else "  (not present)"))
    else:
        _say("  .env               : file not found")

    # what the running process would read
    live = os.environ.get("MKWL_APK_OCTET")
    _say(f"  process env        : {live!r}")

    try:
        from direct import apk_mode
        _say(f"  apk_mode.enabled() : {apk_mode.enabled()}")
    except Exception as exc:  # noqa: BLE001
        bad("1", f"cannot import direct.apk_mode: {type(exc).__name__}: {exc}",
            "the repo is incomplete or broken; reinstall before going further")
        return False

    # the persisted panel setting
    stored = None
    try:
        from bot.store import store
        stored = bool(store.apk_octet)
        _say(f"  panel setting      : {stored}")
    except Exception as exc:  # noqa: BLE001
        warn("1", f"could not read the panel setting: {type(exc).__name__}: {exc}",
             "not fatal for this check; the live env var is what sends files")

    truthy = {"1", "true", "yes", "on"}
    env_on = str(env_val or "").lower() in truthy

    if not apk_mode.enabled():
        if stored:
            bad("1",
                "the panel says APK mode is ON but this process has it OFF",
                "the panel sets the live env var, so the running bot honours it "
                "until it RESTARTS -- then it reads .env, which does not have the "
                "key. Put MKWL_APK_OCTET=1 in .env (at the TOP: the loader keeps "
                "the FIRST occurrence) and restart: systemctl restart mkwlsoso-bot")
        else:
            bad("1", "APK mode is OFF -- .apk uploads use the MIME Eitaa blocks",
                "turn on 📦 APK send mode in Settings, AND add MKWL_APK_OCTET=1 "
                "to the top of .env so it survives a restart")
        good = False
    else:
        ok("1", "APK mode is ON in this process")
        if not env_on:
            warn("1", "it is ON now but NOT set in .env",
                 "the next restart will turn it back OFF. Add "
                 "MKWL_APK_OCTET=1 to the top of .env.")
            good = False

    if stored is not None and stored != apk_mode.enabled():
        warn("1", f"panel setting ({stored}) disagrees with the live flag "
                  f"({apk_mode.enabled()})")

    # What is loaded RIGHT NOW? A dry_run/send uses this, so if it is an .apk and
    # the toggle is off, that combination alone explains an upload that never
    # completes -- see the locate_failed note in stage 5.
    try:
        from bot.store import store as _st
        content = dict(_st.content or {})
        kind = content.get("kind")
        name = content.get("name") or content.get("path") or ""
        _say(f"  loaded content     : kind={kind!r} name={name!r}")
        if kind == "file" and apk_mode.is_apk(name):
            base = mimetypes.guess_type(name)[0] or OCTET
            wire = apk_mode.effective_mime(name, base)
            _say(f"  MIME it would use  : {wire}")
            if wire == BLOCKED:
                bad("1",
                    f"the loaded file is an .apk and it would go out as {BLOCKED}",
                    "this is the blocked MIME. On the bridge path a refused "
                    "document produces NO message, so the upload poll times out "
                    "and you see 'upload_failed / locate_failed' -- which looks "
                    "like a network problem but is this. Turn APK mode ON.")
                good = False
            else:
                ok("1", f"the loaded .apk would go out as {wire} (not blocked)")
    except Exception as exc:  # noqa: BLE001
        warn("1", f"could not read the loaded content: {type(exc).__name__}: {exc}")
    return good


# --------------------------------------------------------------- stage 2

def stage2_policy() -> bool:
    """Does the policy function still rewrite an apk? Pure, offline, no excuses."""
    _hdr(2, "the policy: does effective_mime() rewrite .apk?")
    from direct import apk_mode

    saved = os.environ.get(apk_mode.APK_OCTET_ENV)
    good = True
    try:
        os.environ[apk_mode.APK_OCTET_ENV] = "1"
        got = apk_mode.effective_mime("app.apk", BLOCKED)
        if got == OCTET:
            ok("2", f"ON : app.apk  {BLOCKED} -> {got}")
        else:
            bad("2", f"ON : app.apk was NOT rewritten (got {got!r})",
                "direct/apk_mode.py has been modified; restore effective_mime()")
            good = False

        for name, base in (("photos.zip", "application/zip"),
                           ("doc.pdf", "application/pdf")):
            got = apk_mode.effective_mime(name, base)
            if got == base:
                ok("2", f"ON : {name} untouched ({base})")
            else:
                bad("2", f"ON : {name} was rewritten to {got!r} -- it must not be",
                    "effective_mime must only touch .apk")
                good = False

        os.environ[apk_mode.APK_OCTET_ENV] = "0"
        got = apk_mode.effective_mime("app.apk", BLOCKED)
        if got == BLOCKED:
            ok("2", "OFF: app.apk keeps the OS MIME (byte-identical to old behaviour)")
        else:
            bad("2", f"OFF: app.apk was still rewritten (got {got!r})",
                "the toggle no longer gates the rewrite")
            good = False
    finally:
        if saved is None:
            os.environ.pop(apk_mode.APK_OCTET_ENV, None)
        else:
            os.environ[apk_mode.APK_OCTET_ENV] = saved
    return good


# --------------------------------------------------------------- stage 3

def stage3_wiring() -> bool:
    """Do BOTH send paths still call the policy?

    A source check on purpose: this is the link that breaks silently on an
    upgrade, a merge, or a hand-edit on the server. Nothing at runtime tells you
    the call site vanished -- the send simply uses the blocked MIME again.
    """
    _hdr(3, "the wiring: do the send paths still apply it?")
    good = True
    sites = [
        ("bridge (the engine the panel uses)", "eitaa/driver.py", "bridge_file_init"),
        ("direct/hybrid serializer", "direct/eitaa_tl.py", None),
        ("direct/hybrid sender", "direct/sender.py", None),
    ]
    for label, rel, func in sites:
        path = os.path.join(_ROOT, rel)
        if not os.path.isfile(path):
            bad("3", f"{rel} is missing", "the install is incomplete; reinstall")
            good = False
            continue
        src = open(path, encoding="utf-8", errors="replace").read()
        window = src
        if func:
            i = src.find(f"def {func}")
            if i == -1:
                bad("3", f"{rel}: {func}() not found",
                    "the bridge upload function was renamed or removed")
                good = False
                continue
            # Slice to the END of the function, not a fixed byte window: a fixed
            # window stops covering the code as soon as it grows and then reports
            # a present fix as missing.
            nxt = re.search(r"^\s{0,4}(?:async )?def |^class ", src[i + 10:], re.M)
            window = src[i:i + 10 + (nxt.start() if nxt else len(src))]
        if "effective_mime" in window:
            ok("3", f"{label}: applies effective_mime()  [{rel}]")
        else:
            bad("3", f"{label}: does NOT call effective_mime()  [{rel}]",
                "the fix was lost here. Restore the apk_mode call; see "
                "docs/APK_SEND_STATUS.md 'The fix'")
            good = False
    return good


# --------------------------------------------------------------- stage 4

def stage4_os_mime() -> bool:
    """What does THIS server map .apk to?

    Relevant because the whole bug is that the OS hands the bot the blocked MIME.
    On Debian/Ubuntu /etc/mime.types supplies it. A server that maps .apk to
    something else changes what "OFF" even means.
    """
    _hdr(4, "this server's MIME table")
    guess = mimetypes.guess_type("example.apk")[0]
    _say(f"  mimetypes.guess_type('example.apk') -> {guess!r}")
    if guess == BLOCKED:
        ok("4", "as expected: the OS supplies the MIME Eitaa blocks, so APK mode "
                "is REQUIRED for .apk to deliver")
    elif guess is None:
        warn("4", "this server has no mapping for .apk",
             "the upload would fall back to octet-stream anyway, so .apk may "
             "deliver even with the toggle OFF. Do not rely on it: a package "
             "update can add the mapping and silently break sending again.")
    else:
        warn("4", f"unexpected mapping: {guess!r}",
             "not the blocked type; note it in case it is also filtered")
    return True


# --------------------------------------------------------------- stage 5

def stage5_live(account: str, size_mb: float) -> bool:
    """Ask Eitaa, today, which MIMEs it accepts for identical bytes.

    Identical payload every time; only the filename/MIME changes. Whatever
    diverges is what Eitaa filters on. Delivers ONLY to your own Saved Messages.

    The cases are ordered so the answer is unambiguous:
      control.zip / zip     -- proves uploading works at all right now
      app.apk   / octet     -- THE FIX. If this fails, the recorded fix is dead.
      app.apk   / apk MIME  -- the known-blocked case, as a negative control
      app.apk   / bin       -- a second smuggle candidate, if octet is now blocked
      app.bin   / octet     -- apk bytes under a neutral name
    """
    _hdr(5, "live: what does Eitaa accept today?")
    _say(f"  account: {account}   payload: {size_mb} MB   target: Saved Messages")
    _say("  Nothing is sent to anyone else.")
    _say()

    try:
        import asyncio
        from capture.pool import pool as session_pool
        from eitaa.driver import EitaaDriver
        from config import config
    except Exception as exc:  # noqa: BLE001
        bad("5", f"cannot import the browser stack: {type(exc).__name__}: {exc}",
            "run this on the server, inside .venv, with DISPLAY=:99 set")
        return False

    import tempfile
    from direct import apk_mode

    tmpdir = tempfile.mkdtemp(prefix="apk_triage_")
    blob = os.urandom(1024) + b"\x00" * max(0, int(size_mb * 1024 * 1024) - 1024)

    def _write(name: str) -> str:
        p = os.path.join(tmpdir, name)
        with open(p, "wb") as f:
            f.write(blob)
        return p

    _say(f"  payload: {len(blob):,} bytes, identical for every case")
    _say(f"  budget the code will allow: {min(420.0, 45.0 + 25.0 * size_mb):.0f}s "
         f"per upload (45s + 25s/MB)")
    _say()

    # The MIME is steered through the SAME path the product uses: the driver calls
    # mimetypes.guess_type(), so registering a type for ".apk" in this process
    # controls what goes on the wire without touching product code. APK mode is
    # forced OFF for those cases so effective_mime() does not overwrite the
    # candidate being tested; the one case that tests the real fix turns it ON.
    real_apk_mime = mimetypes.guess_type("x.apk")[0]

    cases = [
        ("control.zip", "application/zip", False, "control: does uploading work at all"),
        ("app.apk", real_apk_mime or BLOCKED, False,
         "the known-blocked case (APK mode OFF) -- reproduces your error"),
        ("app.apk", None, True, "THE FIX: APK mode ON -> octet-stream"),
        ("app.apk", "application/binary", False, "alternative smuggle MIME"),
        ("app.apk", "application/vnd.android.package", False, "near-miss MIME"),
        ("app.bin", OCTET, False, "same bytes, neutral name"),
    ]
    results: list[tuple[str, str, str, str]] = []
    saved_flag = os.environ.get(apk_mode.APK_OCTET_ENV)

    async def run() -> None:
        async with session_pool.lease(account, headed=config.HEADED_JOBS) as session:
            drv = EitaaDriver(session)
            await drv.open()
            if not await drv.is_logged_in():
                bad("5", "this account is not logged in",
                    "add or re-login the account in the panel first")
                return
            peer = None
            try:
                peer = await drv.self_peer_id()
            except Exception as exc:  # noqa: BLE001
                _say(f"  could not resolve own peer: {type(exc).__name__}: {exc}")
            if not peer:
                bad("5", "could not resolve this account's own peer id",
                    "the session is probably stale: run 🩺 Session Check in the panel")
                return
            _say(f"  own peer (Saved Messages): {peer}")
            _say()

            for name, mime, apk_on, why in cases:
                apk_mode.set_env(apk_on)
                if mime is not None:
                    mimetypes.add_type(mime, os.path.splitext(name)[1])
                elif os.path.splitext(name)[1] == ".apk" and real_apk_mime:
                    mimetypes.add_type(real_apk_mime, ".apk")
                base = mimetypes.guess_type(name)[0] or OCTET
                wire = apk_mode.effective_mime(name, base)
                _say(f"  -> {name:14s} wire={wire:46s} {why}")
                path = _write(name)
                t0 = time.time()
                try:
                    # force_mime keeps the payload identical and changes ONLY the
                    # declared type, which is the entire point of the probe.
                    res = await drv.bridge_file_init(path)
                    if not (res or {}).get("ok"):
                        mime = wire
                        code = str((res or {}).get("code")
                                   or (res or {}).get("error") or "")
                        # DISTINGUISH the two failures. They look the same from
                        # the panel (upload_failed) and mean opposite things:
                        #   locate_failed -> sendFile was accepted, getHistory
                        #     worked, but the message NEVER APPEARED. Eitaa took
                        #     the file and refused it server-side; a blocked MIME
                        #     surfaces exactly like this.
                        #   sendFile:/getHistory: -> the page or the network broke
                        #     before Eitaa ever judged the file.
                        state = ("REFUSED-SILENTLY" if "locate_failed" in code
                                 else "UPLOAD-BROKE")
                        results.append((name, mime, state, code[:120]))
                        _say(f"     {state}: {code[:160]}")
                        if state == "REFUSED-SILENTLY":
                            _say("       (accepted for upload, then no message "
                                 "was ever created -- a server-side refusal)")
                        continue
                    # The upload landed. Delivering it to our OWN peer proves the
                    # document is usable, and separates "Eitaa refused the upload"
                    # from "Eitaa refused the send".
                    send = await drv.bridge_file_send(
                        str(peer), caption="apk triage test - please ignore")
                    okk = bool((send or {}).get("ok"))
                    detail = str((send or {}).get("code")
                                 or (send or {}).get("error") or "")[:120]
                    results.append((name, wire,
                                    "SENT" if okk else "SEND-REJECTED", detail))
                    _say(f"     upload OK in {time.time() - t0:.1f}s "
                         f"({(res or {}).get('tries')} checks) -> "
                         f"{'SENT' if okk else 'SEND-REJECTED'}"
                         + (f"  {detail}" if detail else ""))
                except Exception as exc:  # noqa: BLE001 - report, never abort
                    results.append((name, wire, "ERROR",
                                    f"{type(exc).__name__}: {exc}"[:120]))
                    _say(f"     ERROR {type(exc).__name__}: {exc}")

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        bad("5", f"the live probe could not run: {type(exc).__name__}: {exc}",
            "check DISPLAY=:99, that xvfb is up, and that the account is logged in")
        return False
    finally:
        # Leave the toggle exactly as it was: this is a diagnostic, not a setting.
        if saved_flag is None:
            os.environ.pop(apk_mode.APK_OCTET_ENV, None)
        else:
            os.environ[apk_mode.APK_OCTET_ENV] = saved_flag

    if not results:
        return False

    _say()
    _say("  " + "-" * 62)
    _say(f"  {'file':14s} {'mime':46s} result")
    for name, mime, state, _d in results:
        _say(f"  {name:14s} {mime:46s} {state}")
    _say("  " + "-" * 62)

    # Key on (name, wire-mime) as actually observed, not on what was requested.
    by = {(n, m): s for n, m, s, _ in results}
    def _state(fname: str, wire: str) -> str | None:
        return by.get((fname, wire))

    control = _state("control.zip", "application/zip")
    fix = _state("app.apk", OCTET)
    known_bad = _state("app.apk", real_apk_mime or BLOCKED)

    if control != "SENT":
        if control == "UPLOAD-BROKE":
            bad("5", "a plain .zip could not even be handed to Eitaa",
                "the page or the network broke before any filtering. Check the "
                "session, xvfb, and reachability of the media host "
                "fateme.eitaa.com, then re-run")
        elif control == "REFUSED-SILENTLY":
            bad("5", "a plain .zip was accepted then never appeared -- uploads "
                     "are failing for EVERY type, not just apk",
                "this is not the MIME filter. Likely causes, in order: this "
                "account is rate-limited or restricted for media; the media host "
                "is unreachable from here; the payload exceeds a size limit. Run "
                "deploy/upload_sweep.py to find the size boundary, and check the "
                "account with the panel's Session Check")
        else:
            bad("5", f"the .zip control did not send ({control})",
                "fix the control case before drawing any apk conclusion")
        return False
    ok("5", "a plain .zip sends, so uploading works for normal files")

    if fix == "REFUSED-SILENTLY" or known_bad == "REFUSED-SILENTLY":
        _say()
        _say("  NOTE: a 'REFUSED-SILENTLY' above is what the panel reports as")
        _say("  'upload_failed / locate_failed'. It is NOT a timeout to tune --")
        _say("  Eitaa accepted the bytes and then declined to create the message.")

    if fix == "SENT":
        ok("5", "apk + octet-stream DELIVERS -- the recorded fix still works")
        if known_bad == "SENT":
            warn("5", "the real apk MIME was ALSO accepted",
                 "Eitaa may have dropped the filter; APK mode is now harmless "
                 "but no longer necessary")
        _say()
        _say("  => Stages 1-4 told you where the chain was broken. The wire is fine.")
        return True

    bad("5", "apk + octet-stream was REJECTED -- the recorded fix no longer works",
        "Eitaa has tightened its filter. This is a NEW bug, not the old one")
    alts = [(n, m) for (n, m), s in by.items()
            if s == "SENT" and n.endswith(".apk")]
    if alts:
        _say()
        _say("  Still accepted with an .apk NAME:")
        for n, m in alts:
            _say(f"    {n}  {m}")
        _say("  => try one of these as the new smuggle MIME in direct/apk_mode.py")
    elif by.get(("app.bin", OCTET)) == "SENT":
        _say()
        _say("  Only the NEUTRAL NAME got through, so Eitaa is now filtering the")
        _say("  .apk FILENAME as well as the MIME. That is a different fix:")
        _say("  the name in documentAttributeFilename would have to change, which")
        _say("  changes what the recipient receives. Decide before shipping it.")
    else:
        _say()
        _say("  Nothing carrying apk bytes got through. Capture a competitor send")
        _say("  and compare (deploy/eitaa_deep_probe.py) before guessing further.")
    return False


# --------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Triage apk sending: find WHICH link in the chain is broken.")
    ap.add_argument("--live", action="store_true",
                    help="also run the live probe (needs DISPLAY=:99 and a "
                         "logged-in account; delivers only to Saved Messages)")
    ap.add_argument("--account", default=None,
                    help="account for --live, e.g. 989132531349")
    ap.add_argument("--size-mb", type=float, default=2.0,
                    help="probe payload size (default 2)")
    args = ap.parse_args()

    _say("APK SEND TRIAGE")
    _say(f"repo: {_ROOT}")
    _say("Stages 1-4 are offline. Stage 5 needs --live.")

    s1 = stage1_toggle()
    s2 = stage2_policy()
    s3 = stage3_wiring()
    stage4_os_mime()

    if args.live:
        if not args.account:
            _say()
            _say("  --live needs --account <digits>")
            return 2
        stage5_live(args.account, args.size_mb)

    _say()
    _say("=" * 66)
    _say("VERDICT")
    _say("=" * 66)
    broken = [f for f in _findings if f[0] == "BROKEN"]
    warns = [f for f in _findings if f[0] == "WARN"]
    if not broken and not warns:
        _say("  Every offline link is intact.")
        if not args.live:
            _say("  Nothing here explains a failure, so the problem is on the wire.")
            _say("  Run the live probe -- it is the only thing that can tell you")
            _say("  whether Eitaa still accepts octet-stream:")
            _say(f"    DISPLAY=:99 .venv/bin/python deploy/apk_triage.py --live "
                 f"--account <your account>")
    else:
        for sev, stage, msg in broken + warns:
            _say(f"  [{sev}] stage {stage}: {msg}")
        if broken:
            _say()
            _say("  Fix the FIRST item above, then re-run. The links are ordered,")
            _say("  so a later one may only look broken because of an earlier one.")
    _say()
    _say("Background: docs/APK_SEND_STATUS.md")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
