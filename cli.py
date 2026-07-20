"""Command-line entry point for the Eitaa web capture tool.

Commands
--------
  login    Open the browser so you can log in manually. The session is saved
           in the account's isolated profile for later reuse.

  capture  Record one operation: idle baseline -> you perform the action in the
           visible browser -> short trail. Writes a capture run to artifacts/.

  analyze  Turn a capture run into a human-readable report.md.

  list     List existing capture runs.

  inspect  Print a safe, structural snapshot of the Eitaa Web DOM (to confirm
           or fix UI selectors). No message text or personal data is printed.

  send     Send ONE text message to a chat via the browser driver (a real
           end-to-end test of the send path).

This tool automates the OWNER'S OWN accounts only. It does not bypass login,
OTP, CAPTCHA, or rate limits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from config import config
from capture.analyzer import analyze
from capture.browser import open_session
from capture.recorder import RunRecorder
from eitaa.driver import EitaaDriver, inspect_dom


async def cmd_login(account: str) -> int:
    config.ensure_dirs()
    print(f"[login] opening browser for account '{account}'")
    print("[login] log in manually (phone + code). Do NOT share the code with anyone.")
    async with open_session(account) as session:
        await session.goto()
        print("[login] browser is open. Press ENTER here when you have finished logging in...")
        await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
        print("[login] session saved to profile:", session.profile_dir)
    return 0


async def _wait_enter(prompt: str) -> None:
    print(prompt)
    await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)


async def cmd_capture(account: str, op: str, manual: bool) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        await session.goto()
        rec = RunRecorder(session, op)
        await rec.start()
        print(f"[capture] run_id={rec.run_id}")
        print(f"[capture] marker for this run: {rec.marker}")
        print(f"[capture] recording idle baseline ({config.BASELINE_SECONDS}s), do nothing...")
        await rec.baseline()

        async def do_action() -> None:
            # Manual mode: the human performs exactly one operation in the
            # browser window, using the marker text where a message is needed.
            await _wait_enter(
                "[capture] PERFORM THE ACTION NOW in the browser "
                f"(use the marker text: {rec.marker}), then press ENTER..."
            )

        await rec.action(do_action if manual else None)
        run_dir = await rec.finish()
        print(f"[capture] done. artifacts: {run_dir}")
        print(f"[capture] next: python cli.py analyze --run {rec.run_id}")
    return 0


def cmd_analyze(run_id: str) -> int:
    out = analyze(run_id)
    print(f"[analyze] report written: {out}")
    print(out.read_text(encoding="utf-8"))
    return 0


def cmd_list() -> int:
    config.ensure_dirs()
    runs = sorted(p.name for p in config.ARTIFACTS_DIR.iterdir() if p.is_dir())
    if not runs:
        print("(no capture runs yet)")
    for r in runs:
        print(r)
    return 0


async def cmd_inspect(account: str, open_query: str | None) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        logged_in = await driver.is_logged_in()
        print(f"[inspect] logged_in guess: {logged_in}")
        if open_query:
            try:
                await driver.open_chat(open_query)
                print(f"[inspect] opened chat for query: {open_query!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"[inspect] could not open chat: {exc}")
        snapshot = await inspect_dom(session.page)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


async def cmd_chats(account: str) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        titles = await driver.list_chat_titles(limit=20)
        print(f"[chats] {len(titles)} visible chats (use one of these names with --to):")
        for i, t in enumerate(titles, 1):
            print(f"  {i:2d}. {t}")
    return 0


async def cmd_send(account: str, to: str, text: str, no_verify: bool) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[send] not logged in. run: python cli.py login --account", account)
            return 2
        print(f"[send] sending to {to!r} ...")
        result = await driver.send_text(to, text, verify=not no_verify)
        print(f"[send] ok={result.ok} detail={result.detail}")
        return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Eitaa web capture tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="open browser to log in manually")
    p_login.add_argument("--account", required=True)

    p_cap = sub.add_parser("capture", help="record one operation")
    p_cap.add_argument("--account", required=True)
    p_cap.add_argument("--op", required=True, help="label, e.g. send_text, send_contact, list_contacts")
    p_cap.add_argument(
        "--auto",
        action="store_true",
        help="do not wait for a manual action (baseline+trail only)",
    )

    p_an = sub.add_parser("analyze", help="build report.md for a run")
    p_an.add_argument("--run", required=True)

    sub.add_parser("list", help="list capture runs")

    p_insp = sub.add_parser("inspect", help="print structural DOM snapshot")
    p_insp.add_argument("--account", required=True)
    p_insp.add_argument("--open", default=None, help="optional chat name to open before inspecting")

    p_chats = sub.add_parser("chats", help="list visible chat titles (choose one for --to)")
    p_chats.add_argument("--account", required=True)

    p_send = sub.add_parser("send", help="send one text message via the browser driver")
    p_send.add_argument("--account", required=True)
    p_send.add_argument("--to", required=True, help="chat/contact name or username to open")
    p_send.add_argument("--text", required=True, help="message text to send")
    p_send.add_argument("--no-verify", action="store_true", help="skip DOM verification")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "login":
        return asyncio.run(cmd_login(args.account))
    if args.command == "capture":
        return asyncio.run(cmd_capture(args.account, args.op, manual=not args.auto))
    if args.command == "analyze":
        return cmd_analyze(args.run)
    if args.command == "list":
        return cmd_list()
    if args.command == "inspect":
        return asyncio.run(cmd_inspect(args.account, args.open))
    if args.command == "chats":
        return asyncio.run(cmd_chats(args.account))
    if args.command == "send":
        return asyncio.run(cmd_send(args.account, args.to, args.text, args.no_verify))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
