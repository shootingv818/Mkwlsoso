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
from pathlib import Path

from eitaa.driver import EitaaDriver, inspect_dom, inspect_menu
from jobs.campaign import create_campaign, run_campaign, request_stop
from jobs.state import JobState
from capture import deep
from capture.deep import HOOKS_JS
from capture.dossier import build_dossier
from capture.extract_params import extract_run, summarize


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


async def cmd_probe(account: str, op: str, manual: bool) -> int:
    """Deep protocol capture: hooks + raw frames + assets + storage + dossier."""
    config.ensure_dirs()
    async with open_session(account, init_script_path=HOOKS_JS) as session:
        await session.goto()
        rec = RunRecorder(session, f"probe_{op}")
        await rec.start()
        print(f"[probe] run_id={rec.run_id}")
        print(f"[probe] marker: {rec.marker}")
        print(f"[probe] instrumentation injected (fetch/xhr/worker/wasm/crypto).")
        print(f"[probe] idle baseline ({config.BASELINE_SECONDS}s), do nothing...")
        await rec.baseline()
        await deep.pull_hooks(session.page, rec.emit_event)

        async def do_action() -> None:
            await _wait_enter(
                "[probe] PERFORM THE ACTION NOW in the browser "
                f"(login / open contacts / send, using marker {rec.marker} where text is needed), "
                "then press ENTER..."
            )

        await rec.action(do_action if manual else None)

        # Drain the in-page hook buffer (raw frames + crypto/worker/wasm records).
        n = await deep.pull_hooks(session.page, rec.emit_event)
        print(f"[probe] pulled {n} hook records")

        # Download JS/WASM assets and dump storage structure.
        urls = await deep.collect_asset_urls(session.page)
        assets_info = await deep.download_assets(session.context, urls, rec.run_dir / "assets")
        print(f"[probe] downloaded {assets_info['count']} assets -> {assets_info['dir']}")

        storage = await deep.dump_storage(session.page)
        (rec.run_dir / "storage.json").write_text(
            json.dumps(storage, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[probe] storage keys: localStorage={len(storage.get('localStorage_keys', []))} "
              f"indexeddb={len(storage.get('indexeddb', []))}")

        await rec.finish(extra_meta={"deep": True, "assets": assets_info})

    out = build_dossier(rec.run_id)
    print(f"[probe] dossier written: {out}")
    print(out.read_text(encoding="utf-8"))
    return 0


def cmd_extract(run_id: str) -> int:
    out = extract_run(run_id)
    print(f"[extract] params written: {out}")
    print(summarize(out))
    return 0


def cmd_list() -> int:
    config.ensure_dirs()
    runs = sorted(p.name for p in config.ARTIFACTS_DIR.iterdir() if p.is_dir())
    if not runs:
        print("(no capture runs yet)")
    for r in runs:
        print(r)
    return 0


async def cmd_inspect(account: str, open_query: str | None, menu: bool) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        logged_in = await driver.is_logged_in()
        print(f"[inspect] logged_in guess: {logged_in}")
        if menu:
            info = await inspect_menu(session.page)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if open_query:
            try:
                await driver.open_chat(open_query)
                print(f"[inspect] opened chat for query: {open_query!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"[inspect] could not open chat: {exc}")
        snapshot = await inspect_dom(session.page)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


async def cmd_contacts(account: str, out: str) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[contacts] not logged in. run: python cli.py login --account", account)
            return 2
        print("[contacts] opening Contacts view and scrolling the full list...")
        try:
            contacts = await driver.collect_all_contacts()
        except Exception as exc:  # noqa: BLE001
            print(f"[contacts] failed to open contacts view: {exc}")
            print("[contacts] run this to reveal the menu, then send me the output:")
            print(f"           DISPLAY=:99 python cli.py inspect --account {account} --menu")
            return 1
        out_path = Path(out)
        lines = [c["title"] for c in contacts if c.get("title")]
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[contacts] collected {len(lines)} contacts -> {out_path}")
        # Also save structured data (title + peer_id) alongside.
        json_path = out_path.with_suffix(".json")
        json_path.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[contacts] structured data -> {json_path}")
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


async def cmd_collect(account: str, out: str, users_only: bool) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[collect] not logged in. run: python cli.py login --account", account)
            return 2
        print("[collect] scrolling chat list (this can take a bit)...")
        chats = await driver.collect_all_chats()
        if users_only:
            chats = [c for c in chats if c.get("kind") == "user"]
        lines = [c["title"] for c in chats if c.get("title")]
        out_path = Path(out)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[collect] wrote {len(lines)} names to {out_path}")
        print("[collect] EDIT this file to keep only the recipients you want, then run campaign.")
    return 0


def _read_recipients(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


async def cmd_campaign(account: str, file: str | None, text: str | None, resume: str | None, limit: int | None) -> int:
    config.ensure_dirs()
    if resume:
        job = JobState.load(resume)
        print(f"[campaign] resuming {job.job_id} (status was {job.status})")
    else:
        if not file or text is None:
            print("[campaign] need --file and --text to start a new campaign")
            return 2
        names = _read_recipients(file)
        if not names:
            print("[campaign] recipient file is empty")
            return 2
        total = len(names)
        job = create_campaign(account, text, names, limit=limit)
        if limit and limit > 0:
            print(f"[campaign] TEST MODE: using first {len(job.recipients)} of {total} recipients")
        print(f"[campaign] created job {job.job_id} with {len(job.recipients)} recipients")
    await run_campaign(job)
    return 0


def cmd_campaign_status(job_id: str) -> int:
    job = JobState.load(job_id)
    c = job.counts()
    print(f"job {job.job_id} status={job.status}")
    print(f"  total={c['total']} sent={c['sent']} failed={c['failed']} "
          f"pending={c['pending']} skipped={c['skipped']}")
    failed = [r.name for r in job.recipients if r.status == 'failed']
    if failed:
        print(f"  failed ({len(failed)}): {', '.join(failed[:15])}")
    return 0


def cmd_campaign_stop(job_id: str) -> int:
    request_stop(job_id)
    print(f"[campaign] stop flag written for {job_id}. it will stop after the current recipient.")
    return 0


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

    p_probe = sub.add_parser("probe", help="deep protocol capture (hooks + assets + dossier)")
    p_probe.add_argument("--account", required=True)
    p_probe.add_argument("--op", required=True, help="label, e.g. login, contacts, send_text, send_file")
    p_probe.add_argument("--auto", action="store_true", help="no manual action (baseline+trail only)")

    p_ext = sub.add_parser("extract", help="mine MTProto params (DCs/api/layer/RSA) from a run's assets")
    p_ext.add_argument("--run", required=True)

    sub.add_parser("list", help="list capture runs")

    p_insp = sub.add_parser("inspect", help="print structural DOM snapshot")
    p_insp.add_argument("--account", required=True)
    p_insp.add_argument("--open", default=None, help="optional chat name to open before inspecting")
    p_insp.add_argument("--menu", action="store_true", help="open the sidebar menu and dump its items")

    p_chats = sub.add_parser("chats", help="list visible chat titles (choose one for --to)")
    p_chats.add_argument("--account", required=True)

    p_contacts = sub.add_parser("contacts", help="collect ALL contacts from the Contacts view")
    p_contacts.add_argument("--account", required=True)
    p_contacts.add_argument("--out", default="contacts.txt", help="output file path")

    p_send = sub.add_parser("send", help="send one text message via the browser driver")
    p_send.add_argument("--account", required=True)
    p_send.add_argument("--to", required=True, help="chat/contact name or username to open")
    p_send.add_argument("--text", required=True, help="message text to send")
    p_send.add_argument("--no-verify", action="store_true", help="skip DOM verification")

    p_col = sub.add_parser("collect", help="scroll all chats and write names to a file")
    p_col.add_argument("--account", required=True)
    p_col.add_argument("--out", default="recipients_all.txt", help="output file path")
    p_col.add_argument("--users-only", action="store_true", help="exclude groups/channels")

    p_camp = sub.add_parser("campaign", help="broadcast a text to a recipient list")
    p_camp.add_argument("--account", required=True)
    p_camp.add_argument("--file", default=None, help="recipients file (one name per line)")
    p_camp.add_argument("--text", default=None, help="message text to broadcast")
    p_camp.add_argument("--resume", default=None, help="resume an existing job_id")
    p_camp.add_argument("--limit", type=int, default=None, help="only send to the first N recipients (safe test)")

    p_cs = sub.add_parser("campaign-status", help="show a campaign's progress")
    p_cs.add_argument("--job", required=True)

    p_cx = sub.add_parser("campaign-stop", help="request a campaign to stop cleanly")
    p_cx.add_argument("--job", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "login":
        return asyncio.run(cmd_login(args.account))
    if args.command == "capture":
        return asyncio.run(cmd_capture(args.account, args.op, manual=not args.auto))
    if args.command == "analyze":
        return cmd_analyze(args.run)
    if args.command == "probe":
        return asyncio.run(cmd_probe(args.account, args.op, manual=not args.auto))
    if args.command == "extract":
        return cmd_extract(args.run)
    if args.command == "list":
        return cmd_list()
    if args.command == "inspect":
        return asyncio.run(cmd_inspect(args.account, args.open, args.menu))
    if args.command == "chats":
        return asyncio.run(cmd_chats(args.account))
    if args.command == "contacts":
        return asyncio.run(cmd_contacts(args.account, args.out))
    if args.command == "send":
        return asyncio.run(cmd_send(args.account, args.to, args.text, args.no_verify))
    if args.command == "collect":
        return asyncio.run(cmd_collect(args.account, args.out, args.users_only))
    if args.command == "campaign":
        return asyncio.run(cmd_campaign(args.account, args.file, args.text, args.resume, args.limit))
    if args.command == "campaign-status":
        return cmd_campaign_status(args.job)
    if args.command == "campaign-stop":
        return cmd_campaign_stop(args.job)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
