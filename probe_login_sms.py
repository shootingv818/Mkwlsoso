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

# Human-readable meaning of the sentCode / codeType constructors.
_TYPE_NOTE = {
    "auth.sentCodeTypeApp": "IN-APP — code pushed to another logged-in device (your phone). NOT an SMS.",
    "auth.sentCodeTypeSms": "SMS — code texted to the number. THIS is what you want.",
    "auth.sentCodeTypeCall": "CALL — code read out over a voice call.",
    "auth.sentCodeTypeFlashCall": "FLASH-CALL — code is the caller's number; needs the app to read the call log.",
    "auth.sentCodeTypeMissedCall": "MISSED-CALL — code is in the calling number of a missed call.",
    "auth.sentCodeTypeFragmentSms": "FRAGMENT — code via the Fragment/anonymous-number service.",
    "auth.sentCodeTypeSetUpEmailRequired": "EMAIL setup required first.",
    "auth.sentCodeTypeEmailCode": "EMAIL — code sent to a linked email.",
    "codeTypeSms": "SMS",
    "codeTypeCall": "CALL",
    "codeTypeFlashCall": "FLASH-CALL",
    "codeTypeMissedCall": "MISSED-CALL",
}

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
    return _TYPE_NOTE.get(ctor, "(unrecognised constructor)")


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
    print("  Read-only-ish: it requests login codes (which really are sent), but")
    print("  never signs in and never touches the bot's own login flow.")
    if args.resend:
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
        variant = args.settings if not args.resend else "empty"
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

        _verdict_after_sendcode(cur, nxt)

        # ---- Stage B: resendCode (one more request) -------------------------
        if args.resend:
            if not phch:
                print("\n  [!] no phone_code_hash returned; cannot resend.")
                return 1
            wait = int(sent.get("timeout") or 0)
            if wait > 0:
                w = min(wait, args.max_wait)
                print(f"\n  waiting {w}s before resend (server said {wait}s)...")
                await asyncio.sleep(w)
            banner("STAGE B — auth.resendCode   (force the next_type)")
            rr = await driver.page.evaluate(
                "(a) => window.__MKWL_probeResendCode(a.p, a.h)",
                {"p": phone, "h": phch})
            if not rr.get("ok"):
                code = str(rr.get("code"))
                print(f"  RESULT: FAILED — {code}")
                fw = flood_seconds(code)
                if fw is not None:
                    print(f"  >>> FLOOD_WAIT: rate-limited for {fw}s. Wait it out.")
                return 1
            sent2 = rr.get("sent") or {}
            cur2 = sent2.get("type")
            print(f"  RESULT: ok")
            print(f"    re-delivered as : {cur2}")
            print(f"                      -> {note_for(cur2)}")
            print(f"    next_type       : {sent2.get('next_type')}")
            print(f"    raw reply       : {json.dumps(sent2, ensure_ascii=False)}")
            _verdict_after_resend(cur, cur2)

    banner("DONE")
    print("  Nothing was signed in and the bot's login flow was not touched.")
    print("  If SMS was achieved above, the bot could offer a 'send by SMS' button")
    print("  that calls auth.resendCode — say the word and I will wire it in,")
    print("  isolated and behind a toggle, with the same rate-limit guards.")
    return 0


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
