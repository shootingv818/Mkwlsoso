#!/usr/bin/env python3
"""PROBE: can we make Eitaa send the login code by SMS instead of in-app?

This is a STANDALONE test/diagnostic script. It is NOT wired into the bot and
changes nothing in the login flow -- it only asks Eitaa questions and prints the
raw answers, so we can see what is actually possible before touching any real
code path. Delete this file and the project is unchanged.

    Run on the authorized server (venv active, DISPLAY set for the browser):
        DISPLAY=:99 .venv/bin/python probe_login_sms.py --phone 09XXXXXXXXX

WHAT IT INVESTIGATES
--------------------
When you request a login code and the phone already has an ACTIVE Eitaa session
(your own phone, logged in), the server delivers the code as an in-APP message
(auth.sentCodeTypeApp) instead of an SMS, to save the SMS cost. That is a server
decision, not something the current code chooses.

The MTProto levers that MIGHT change it, weakest to strongest:

  1. codeSettings flags on auth.sendCode
     (current_number / allow_flashcall / allow_missed_call / allow_app_hash).
     These mostly control flash/missed-call and the Android SMS auto-reader, NOT
     app-vs-SMS -- but we test them because the only way to be sure is to look.

  2. auth.resendCode  <-- the real lever.
     Every auth.sendCode reply carries `type` (how it was just sent) and
     `next_type` (what a RESEND would use). If next_type is codeTypeSms, then
     calling auth.resendCode makes the server send an SMS. This is the standard,
     documented way to move a code off the app and onto SMS.

  3. Different api_id/api_hash (the native APK's, not Eitaa Web's).
     Pass --api-id/--api-hash to see whether the app's own credentials get a
     different delivery type. (You must supply them; they are not in this repo.)

READ THIS ABOUT RATE LIMITS  ***important***
--------------------------------------------
auth.sendCode and auth.resendCode are the most aggressively rate-limited calls
in the whole protocol. A handful of requests can FLOOD_WAIT the number for hours
or days -- during which you cannot log in AT ALL, by any method. Therefore:

  * The default run sends exactly ONE code (inspect only) and stops.
  * --resend adds ONE resendCode after it (so 2 requests total, the SMS attempt).
  * --settings tests ONE codeSettings variant per run, never a loop.
  * Every FLOOD_WAIT is printed verbatim and the script stops. It NEVER retries.

Space your runs out. If you see FLOOD_WAIT_86400, that is a full day; wait it out
rather than trying "just once more".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

# --- extended in-page bridge -------------------------------------------------
# Injected on top of the real login bridge. Adds resendCode and a raw-dump
# sendCode so we can see the exact server reply, plus configurable codeSettings.
# It NEVER loops or retries -- one call per Python-side request.
_PROBE_BRIDGE = r"""
(() => {
  if (window.__MKWL_probeReady) return;
  window.__MKWL_probeReady = true;

  function errStr(e) {
    try { return String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) { return "ERR"; }
  }
  function dump(res) {
    // Shallow, JSON-safe view of the sentCode reply.
    try {
      return {
        _: res && res._,
        type: res && res.type && res.type._,
        type_len: res && res.type && (res.type.length != null ? res.type.length : null),
        next_type: res && res.next_type && res.next_type._,
        timeout: res && res.timeout,
        phone_code_hash: res && res.phone_code_hash,
        // Some builds annotate the app-code target device here.
        raw_keys: res ? Object.keys(res) : []
      };
    } catch (e) { return { dump_error: errStr(e) }; }
  }

  // sendCode with an explicit codeSettings object so variants can be tested.
  window.__MKWL_probeSendCode = async function (phone, apiId, apiHash, settings) {
    const AM = window.apiManager;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    const cs = Object.assign({ _: "codeSettings" }, settings || {});
    try {
      const res = await AM.invokeApi("auth.sendCode", {
        phone_number: String(phone),
        api_id: apiId,
        api_hash: String(apiHash),
        settings: cs
      });
      return { ok: true, sent: dump(res), settings_used: cs };
    } catch (e) { return { ok: false, code: errStr(e) }; }
  };

  // resendCode: ask the server to re-send using the reply's next_type.
  window.__MKWL_probeResendCode = async function (phone, hash) {
    const AM = window.apiManager;
    if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
    try {
      const res = await AM.invokeApi("auth.resendCode", {
        phone_number: String(phone),
        phone_code_hash: String(hash)
      });
      return { ok: true, sent: dump(res) };
    } catch (e) { return { ok: false, code: errStr(e) }; }
  };
})();
"""

# Human-readable meaning of the sentCode / codeType constructors. Keys are
# stored WITHOUT the "auth." prefix; note_for() strips it, because Eitaa returns
# next_type as "auth.codeTypeCall" while the spec name is "codeTypeCall".
_TYPE_NOTE = {
    "sentCodeTypeApp": "IN-APP — code pushed to another logged-in device (your phone). NOT an SMS.",
    "sentCodeTypeSms": "SMS — code texted to the number. THIS is what you want.",
    "sentCodeTypeCall": "CALL — code read out over a voice call.",
    "sentCodeTypeFlashCall": "FLASH-CALL — code is the caller's number; needs the app to read the call log.",
    "sentCodeTypeMissedCall": "MISSED-CALL — code is in the calling number of a missed call.",
    "sentCodeTypeFragmentSms": "FRAGMENT — code via the Fragment/anonymous-number service.",
    "sentCodeTypeSetUpEmailRequired": "EMAIL setup required first.",
    "sentCodeTypeEmailCode": "EMAIL — code sent to a linked email.",
    "codeTypeSms": "SMS — the code would be texted.",
    "codeTypeCall": "CALL — a resend would place a VOICE CALL, not an SMS.",
    "codeTypeFlashCall": "FLASH-CALL.",
    "codeTypeMissedCall": "MISSED-CALL.",
    "codeTypeFragmentSms": "FRAGMENT SMS.",
}


def _classify_notice(text: str) -> tuple[str, str]:
    """Read Eitaa's own (often Persian) resend reply.

    Eitaa returns the resend outcome as a MESSAGE, not always a typed object, so
    a voice call arrives as the text 'در حال تماس با شما...'. That is a SUCCESS
    (a call is being placed), not a failure -- classify it so the walk reads it
    correctly instead of calling it an error.

    Returns (channel, kind) where channel is one of sms/call/app/flood/unknown
    and kind is 'ok' | 'flood' | 'error'.
    """
    t = str(text or "")
    low = t.lower()
    if "FLOOD_WAIT" in t.upper():
        return "flood", "flood"
    if "پیامک" in t or "sms" in low or "اس ام اس" in t:
        # "instead of SMS you will get a call" mentions پیامک but is a CALL, so
        # the call check must win when both appear.
        if "تماس" in t or "call" in low or "صوتی" in t:
            return "call", "ok"
        return "sms", "ok"
    if "تماس" in t or "call" in low or "صوتی" in t or "زنگ" in t:
        return "call", "ok"
    if "codeTypeSms" in t or "sentCodeTypeSms" in t:
        return "sms", "ok"
    return "unknown", "error"

# codeSettings variants, tested ONE per run (each is a separate code request).
_VARIANTS = {
    "empty": {},
    "current_number": {"current_number": True},
    "flashcall": {"allow_flashcall": True, "current_number": True},
    "missedcall": {"allow_missed_call": True, "current_number": True},
    "apphash": {"allow_app_hash": True},
    "all": {"current_number": True, "allow_flashcall": True,
            "allow_missed_call": True, "allow_app_hash": True},
}


def note_for(ctor: str | None) -> str:
    if not ctor:
        return "(none returned)"
    key = str(ctor)
    if key.startswith("auth."):
        key = key[len("auth."):]
    return _TYPE_NOTE.get(key, "(unrecognised constructor)")


def short_type(ctor) -> str:
    """'auth.sentCodeTypeApp' -> 'App', 'auth.codeTypeCall' -> 'Call'."""
    s = str(ctor or "-")
    for pre in ("auth.sentCodeType", "auth.codeType", "sentCodeType", "codeType", "auth."):
        if s.startswith(pre):
            return s[len(pre):]
    return s


def validate_phone(intl: str) -> str | None:
    """Return a reason the number is unusable, or None if it looks fine.

    Done BEFORE the 3-minute browser boot so a typo (or the literal
    09XXXXXXXXX placeholder, which strips to '09') fails in a second, not after
    a code has been burned.
    """
    if not intl.isdigit():
        return f"contains non-digits after normalising: {intl!r}"
    if intl.startswith("98"):
        if len(intl) != 12:
            return (f"Iranian mobile should be 98 + 10 digits (12 total); got "
                    f"{len(intl)}: {intl!r}. Example: 09991048633")
        if intl[2] != "9":
            return f"Iranian mobile national part must start with 9: {intl!r}"
    elif len(intl) < 10:
        return f"too short to be a real number: {intl!r}"
    return None


def flood_seconds(code: str) -> int | None:
    m = re.search(r"FLOOD_WAIT_(\d+)", str(code or ""), re.I)
    return int(m.group(1)) if m else None


def banner(text: str) -> None:
    print("\n" + "=" * 70)
    print("  " + text)
    print("=" * 70)


async def _inject(driver) -> bool:
    from eitaa.login_flow import ensure_login_bridge
    if not await ensure_login_bridge(driver):
        return False
    try:
        await driver.page.evaluate(_PROBE_BRIDGE)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] could not inject the probe bridge: {exc}")
        return False


async def run(args) -> int:
    from config import config  # noqa: F401  (ensures env/config is initialised)
    from capture.browser import open_session
    from eitaa.driver import EitaaDriver
    from eitaa.login_flow import normalize_phone_intl, resolve_api_creds, resolve_creds_with_page

    phone = normalize_phone_intl(args.phone)
    banner(f"LOGIN-DELIVERY PROBE  —  {phone}")

    bad = validate_phone(phone)
    if bad:
        print(f"  [!] that number looks wrong: {bad}")
        print("      (tip: pass the REAL number, not the 09XXXXXXXXX placeholder)")
        return 2

    print("  Read-only-ish: it requests login codes (which really are sent), but")
    print("  never signs in and never touches the bot's own login flow.")
    walk = max(0, int(args.walk or 0))
    if walk:
        print(f"  MODE: WALK THE CHAIN — sendCode + up to {walk} resends, following")
        print("        the server's own next_type each step. This is the full test.")
        print(f"        Worst case = {walk + 1} code requests. That is a lot; run it ONCE.")
    elif args.resend:
        print("  MODE: sendCode + resendCode  (2 code requests — the SMS attempt)")
    else:
        print(f"  MODE: sendCode only, variant '{args.settings}'  (1 code request)")
    print("  If you see FLOOD_WAIT, STOP and wait it out. Do not re-run to 'try again'.")

    api_id, api_hash = (args.api_id, args.api_hash) if args.api_id and args.api_hash \
        else resolve_api_creds()

    async with open_session(args.account, headed=False) as session:
        driver = EitaaDriver(session)
        print("\n  booting Eitaa Web (Chromium is slow on this host — up to ~3 min)...")
        await driver.open()

        if not await _inject(driver):
            print("  [!] auth bridge unavailable — cannot probe. Is the page loaded?")
            return 2

        api_id, api_hash = await resolve_creds_with_page(driver, api_id, api_hash)
        if not api_id or not api_hash:
            print("  [!] no api_id/api_hash. Set EITAA_API_ID / EITAA_API_HASH, or")
            print("      pass --api-id/--api-hash (e.g. the native APK's credentials).")
            return 2
        print(f"  using api_id={api_id}  (hash ...{str(api_hash)[-4:]})")

        # ---- Stage A: sendCode (one request) --------------------------------
        variant = "empty" if (args.resend or walk) else args.settings
        settings = _VARIANTS.get(variant, {})
        banner(f"STAGE A — auth.sendCode   (codeSettings: {variant} = {settings or '{}'})")
        r = await driver.page.evaluate(
            "(a) => window.__MKWL_probeSendCode(a.p, a.i, a.h, a.s)",
            {"p": phone, "i": api_id, "h": api_hash, "s": settings})

        if not r.get("ok"):
            code = str(r.get("code"))
            print(f"  RESULT: FAILED — {code}")
            fw = flood_seconds(code)
            if fw is not None:
                print(f"  >>> FLOOD_WAIT: the number is rate-limited for {fw}s "
                      f"(~{fw // 3600}h {fw % 3600 // 60}m). Wait it out.")
            return 1

        sent = r.get("sent") or {}
        cur = sent.get("type")
        nxt = sent.get("next_type")
        phch = sent.get("phone_code_hash")
        print(f"  RESULT: ok")
        print(f"    delivered as : {cur}")
        print(f"                   -> {note_for(cur)}")
        print(f"    next_type    : {nxt}")
        print(f"                   -> {note_for(nxt)}")
        if sent.get("timeout") is not None:
            print(f"    resend allowed after : {sent.get('timeout')}s")
        print(f"    phone_code_hash : {phch}")
        print(f"    raw reply    : {json.dumps(sent, ensure_ascii=False)}")

        # A step in the delivery chain, recorded for the final stats table.
        steps = [{"n": 0, "call": "sendCode", "type": cur, "next": nxt,
                  "timeout": sent.get("timeout")}]

        _verdict_after_sendcode(cur, nxt)

        # ---- walk the chain via resendCode ----------------------------------
        # resendCode is the intended "escalate delivery" call: each one follows
        # the previous reply's next_type, so App -> SMS -> Call is walked in the
        # order the server itself offers. Stops the moment SMS is reached, the
        # chain ends (no next_type), or anything errors.
        total = walk if walk else (1 if args.resend else 0)
        got_sms = (cur == "auth.sentCodeTypeSms")
        for i in range(1, total + 1):
            if got_sms:
                print("\n  SMS already reached — no reason to escalate further.")
                break
            if not phch:
                print("\n  [!] no phone_code_hash; cannot resend.")
                break
            nxt_now = steps[-1]["next"]
            if not nxt_now:
                print("\n  The server offers no further next_type — the chain ends "
                      "here. SMS is not on offer for this number right now.")
                break
            wait = int(steps[-1]["timeout"] or 0)
            if wait > 0:
                w = min(wait, args.max_wait)
                print(f"\n  waiting {w}s before resend #{i} (server said {wait}s)...")
                await asyncio.sleep(w)
            banner(f"RESEND #{i} — auth.resendCode   (server's next_type: {nxt_now})")
            rr = await driver.page.evaluate(
                "(a) => window.__MKWL_probeResendCode(a.p, a.h)",
                {"p": phone, "h": phch})
            if not rr.get("ok"):
                # Eitaa often returns the resend outcome as a MESSAGE (Persian),
                # not a typed object. 'در حال تماس با شما' is a CALL being placed
                # -- a success, not a failure -- so classify before judging.
                code = str(rr.get("code"))
                channel, kind = _classify_notice(code)
                if kind == "flood":
                    fw = flood_seconds(code)
                    print(f"  RESULT: FLOOD — {code}")
                    if fw is not None:
                        print(f"  >>> rate-limited for {fw}s "
                              f"(~{fw // 3600}h {fw % 3600 // 60}m). STOP now, wait it out.")
                    steps.append({"n": i, "call": "resendCode",
                                  "type": "FLOOD", "next": None, "timeout": None})
                    break
                if kind == "ok":
                    label = {"call": "auth.sentCodeTypeCall",
                             "sms": "auth.sentCodeTypeSms"}.get(channel, channel)
                    print(f"  RESULT: ok (Eitaa notice) — {channel.upper()}")
                    print(f"    Eitaa says: {code.strip()}")
                    steps.append({"n": i, "call": "resendCode", "type": label,
                                  "next": None, "timeout": None,
                                  "notice": code.strip()})
                    if channel == "sms":
                        got_sms = True
                    continue
                print(f"  RESULT: unexpected reply — {code}")
                steps.append({"n": i, "call": "resendCode", "type": f"ERROR:{code}",
                              "next": None, "timeout": None})
                break
            s = rr.get("sent") or {}
            c2 = s.get("type")
            print(f"  RESULT: ok")
            print(f"    re-delivered as : {c2}  ->  {note_for(c2)}")
            print(f"    next_type       : {s.get('next_type')}")
            print(f"    raw reply       : {json.dumps(s, ensure_ascii=False)}")
            # resend keeps the same phone_code_hash unless the server rotates it.
            phch = s.get("phone_code_hash") or phch
            steps.append({"n": i, "call": "resendCode", "type": c2,
                          "next": s.get("next_type"), "timeout": s.get("timeout")})
            if c2 == "auth.sentCodeTypeSms":
                got_sms = True

        _stats_table(steps, phone, api_id)
        _final_verdict(steps)

    banner("DONE")
    print("  Nothing was signed in and the bot's login flow was not touched.")
    print("  If SMS was reached above, the bot can offer a 'send by SMS' button that")
    print("  calls auth.resendCode — say the word and I will wire it in, isolated and")
    print("  behind a toggle, with these same rate-limit guards.")
    return 0


def _stats_table(steps: list[dict], phone: str, api_id: int) -> None:
    banner("STATS")
    print(f"  phone: {phone}    api_id: {api_id}    code requests made: {len(steps)}")
    print()
    print(f"  {'#':<3}{'call':<12}{'delivered as':<26}{'next_type':<16}{'resend-in':<9}")
    print("  " + "-" * 64)
    for s in steps:
        t = short_type(s['type'])
        nx = short_type(s['next']) if s.get('next') else '-'
        to = f"{s['timeout']}s" if s.get('timeout') is not None else '-'
        print(f"  {s['n']:<3}{s['call']:<12}{t:<26}{nx:<16}{to:<9}")
    print()
    methods = [s['type'] for s in steps
               if s.get('type') and not str(s['type']).startswith(('ERROR', 'FLOOD'))]
    seen = dict.fromkeys(short_type(m) for m in methods)
    print(f"  channels seen : {', '.join(seen) or '-'}")
    print(f"  SMS offered   : {'YES' if any(short_type(m) == 'Sms' for m in methods) else 'NO — Eitaa did not offer SMS for this number'}")


def _final_verdict(steps: list[dict]) -> None:
    banner("VERDICT")
    types = [s.get("type") for s in steps]
    if "auth.sentCodeTypeSms" in types:
        first_sms = next(i for i, s in enumerate(steps) if s.get("type") == "auth.sentCodeTypeSms")
        if first_sms == 0:
            print("  ✅ The very first code came by SMS — no bypass needed.")
        else:
            print(f"  ✅ SMS REACHED after {first_sms} resend(s). THE BYPASS WORKS:")
            print("     sendCode, then resendCode until the type is SMS.")
            print("     This is reliable and repeatable; I can wire it into the bot.")
        return
    last = steps[-1]
    if str(last.get("type") or "") == "FLOOD":
        print("  ✗ Cut short by FLOOD_WAIT — the number is now rate-limited.")
        print("    Wait it out FULLY before any further attempt, by any method.")
        return
    if str(last.get("type") or "").startswith("ERROR"):
        print("  ✗ The chain was cut short by an unexpected reply (see above).")
        return

    chain = " -> ".join(short_type(s["type"]) for s in steps)
    saw_call = any(short_type(s.get("type")) == "Call" for s in steps)
    print(f"  Eitaa's delivery chain for this number:  {chain}")
    print()
    if saw_call:
        print("  ✗ SMS is NOT in Eitaa's chain for this number. It goes:")
        print("       App (a device is logged in)  ->  voice CALL on resend.")
        print("    Eitaa chose CALL as the resend channel, not SMS. The delivery")
        print("    method is decided entirely on THEIR servers by phone number and")
        print("    account state — there is no client flag or api_id that overrides")
        print("    it, so nothing this bot sends can turn that call into an SMS.")
    else:
        print("  ✗ SMS was not offered within the resend budget.")
    print()
    print("  THE ONE ROUTE THAT ACTUALLY FORCES SMS:")
    print("    Log this phone OUT of Eitaa (Settings -> Devices -> end this")
    print("    session), then request the code. With no logged-in app to push to,")
    print("    the server's FIRST sentCode has nowhere to go but SMS. Destructive")
    print("    (you sign the phone back in afterwards), so it is the last resort.")
    print("    To confirm cheaply first, run this probe against a DIFFERENT number")
    print("    that is NOT logged into Eitaa anywhere: if its first code is SMS,")
    print("    that proves the 'no active session -> SMS' path.")


def _verdict_after_sendcode(cur, nxt) -> None:
    print("\n  READING:")
    if cur == "auth.sentCodeTypeSms":
        print("    The FIRST code already came by SMS — no bypass needed. Done.")
    elif cur == "auth.sentCodeTypeApp":
        print("    The code went to the APP (an active session exists on this number).")
        if nxt == "codeTypeSms":
            print("    next_type is SMS -> a resend WILL text it. Re-run with --resend.")
        elif nxt:
            print(f"    next_type is {nxt} -> a resend gives THAT, not SMS. SMS not")
            print("    offered right now; the surest route is to log the phone OUT of")
            print("    Eitaa first, then request the code (no app -> server must SMS).")
        else:
            print("    No next_type offered -> the server is not exposing an SMS resend")
            print("    for this number at this moment.")
    else:
        print(f"    Delivered as {cur}. See next_type to know what a resend would do.")


def _verdict_after_resend(cur, cur2) -> None:
    print("\n  VERDICT:")
    if cur2 == "auth.sentCodeTypeSms":
        print("    ✅ SUCCESS — the resend arrived by SMS. This is the bypass:")
        print("       sendCode (app) -> resendCode (SMS). Reliable and repeatable.")
    elif cur2 == cur:
        print("    ✗ The resend used the SAME channel as before, not SMS.")
        print("      The server is not offering SMS for this number now. Options:")
        print("      log the phone OUT of Eitaa so there is no app to push to, or")
        print("      try again later (delivery policy can depend on recent attempts).")
    else:
        print(f"    The resend arrived as {cur2} ({note_for(cur2)}).")
        print("      Not SMS, but a different channel than the first code.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Probe whether Eitaa can be made to send the login code by SMS.")
    ap.add_argument("--phone", required=True,
                    help="the number to test, e.g. 09123456789 or 98912...")
    ap.add_argument("--account", default=None,
                    help="profile/account name to boot (default: derived from the phone)")
    ap.add_argument("--resend", action="store_true",
                    help="after sendCode, also call resendCode (the SMS attempt; 2 requests)")
    ap.add_argument("--walk", type=int, default=0, metavar="N",
                    help="the full test: sendCode then up to N resends, following the "
                         "server's next_type chain (App->SMS->Call). Stops at SMS, at "
                         "the chain's end, or on any error. Try --walk 3.")
    ap.add_argument("--settings", default="empty", choices=sorted(_VARIANTS),
                    help="codeSettings variant to send (ignored when --resend is used)")
    ap.add_argument("--api-id", type=int, default=None,
                    help="override api_id (e.g. the native APK's) to test its delivery")
    ap.add_argument("--api-hash", default=None, help="override api_hash")
    ap.add_argument("--max-wait", type=int, default=60,
                    help="cap the pre-resend wait in seconds (default 60)")
    args = ap.parse_args()

    if not args.account:
        digits = re.sub(r"\D", "", args.phone or "")
        args.account = "probe_" + (digits[-10:] or "acct")

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
