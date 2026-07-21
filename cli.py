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
import os
import sys

from config import config
from capture.analyzer import analyze
from capture.browser import open_session
from capture.recorder import RunRecorder
from pathlib import Path

import csv
import re

from eitaa.driver import (
    EitaaDriver, inspect_dom, inspect_menu, inspect_add_contact, inspect_attach, inspect_login,
)
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


async def cmd_inspect(
    account: str,
    open_query: str | None,
    menu: bool,
    add_contact: bool,
    attach: bool = False,
    attach_file: str | None = None,
    login: bool = False,
) -> int:
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
        if add_contact:
            info = await inspect_add_contact(driver)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if attach:
            print("[inspect] opening Saved Messages (safe) and revealing the upload UI...")
            info = await inspect_attach(driver, file_path=attach_file)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if login:
            print("[inspect] dumping the auth/login page structure...")
            info = await inspect_login(driver)
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


def _normalize_ir_phone(raw: str) -> str | None:
    """Normalize an Iranian mobile number to +98XXXXXXXXXX. None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    # Iranian mobile numbers are 10 digits starting with 9.
    if len(digits) == 10 and digits.startswith("9"):
        return "+98" + digits
    return None


def _read_contacts_csv(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if not row or not row[0].strip():
                continue
            # Skip a header line if present.
            if i == 0 and row[0].strip().lower() in {"phone", "شماره", "number"}:
                continue
            phone = row[0].strip()
            first = row[1].strip() if len(row) > 1 else ""
            last = row[2].strip() if len(row) > 2 else ""
            rows.append({"phone": phone, "first": first, "last": last})
    return rows


async def cmd_add_contacts(account: str, file: str, limit: int | None) -> int:
    config.ensure_dirs()
    entries = _read_contacts_csv(file)
    if limit and limit > 0:
        entries = entries[:limit]
    if not entries:
        print("[add-contacts] no rows in", file)
        return 2

    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[add-contacts] not logged in. run: python cli.py login --account", account)
            return 2

        # Normalize first; keep invalid ones out of the browser batch.
        valid_entries = []
        results = []
        for e in entries:
            norm = _normalize_ir_phone(e["phone"])
            if norm is None:
                results.append({**e, "status": "invalid_number", "detail": "bad phone format"})
                print(f"[add-contacts] {e['phone']} -> invalid_number")
            else:
                valid_entries.append({"phone": norm, "first": e["first"], "last": e["last"]})

        batch_results = await driver.add_contacts_batch(valid_entries)
        for r in batch_results:
            results.append(r)
            print(f"[add-contacts] {r['phone']} ({r['first']} {r['last']}) -> {r['status']} {r.get('detail','')}")

        added = sum(1 for r in results if r["status"] == "added")
        not_on = sum(1 for r in results if r["status"] == "not_on_eitaa")
        invalid = sum(1 for r in results if r["status"] == "invalid_number")
        errors = sum(1 for r in results if r["status"] == "error")
        print(f"[add-contacts] summary: added={added} not_on_eitaa={not_on} "
              f"invalid={invalid} error={errors} total={len(results)}")
    return 0


def _summarize_diagnostic_run(run_dir) -> None:
    """Read the run's events.jsonl and print a self-contained diagnosis.

    The user runs on a headless server and cannot open the artifact files, so
    the key evidence (did the click reach the Add button? did a request fire?)
    is summarized straight to stdout.
    """
    import urllib.parse as _url
    from pathlib import Path as _P

    events_path = _P(run_dir) / "events.jsonl"
    if not events_path.exists():
        print("[diagnose-add-contact] no events.jsonl to analyze")
        return

    ui, http_req, console = [], [], []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        src = ev.get("source")
        if src == "ui":
            ui.append(ev)
        elif src == "http" and ev.get("kind") == "request":
            http_req.append(ev)
        elif src == "console":
            console.append(ev)

    def _is_add_button(ev: dict) -> bool:
        tgt = ev.get("target") or {}
        action = tgt.get("action") or {}
        cls = f"{tgt.get('cls', '')} {action.get('cls', '')}".lower()
        return "btn-primary" in cls or "btn-color-primary" in cls

    clicks = [e for e in ui if e.get("kind") == "click" and e.get("phase") in ("action", "trail")]
    add_clicks = [e for e in clicks if _is_add_button(e)]
    trusted_add = [e for e in add_clicks if e.get("trusted")]

    submit_http = [e for e in http_req if e.get("phase") in ("action", "trail")]
    hosts: dict[str, int] = {}
    for e in submit_http:
        try:
            host = _url.urlparse(e.get("url", "")).netloc or "?"
        except Exception:  # noqa: BLE001
            host = "?"
        hosts[host] = hosts.get(host, 0) + 1

    print("[diagnose-add-contact] --- evidence summary ---")
    print(f"  clicks (submit window): {len(clicks)} | on Add button: {len(add_clicks)} | trusted+on-Add: {len(trusted_add)}")
    if add_clicks:
        tgt = add_clicks[0].get("target", {}) or {}
        act = tgt.get("action") or tgt
        print(f"  add-button target: tag={act.get('tag')} cls={str(act.get('cls',''))[:70]} text={str(act.get('text',''))[:24]}")
    print(f"  HTTP requests after click: {len(submit_http)}")
    if hosts:
        top = ", ".join(f"{h}({n})" for h, n in sorted(hosts.items(), key=lambda x: -x[1])[:6])
        print(f"  request hosts: {top}")
    print(f"  console warnings/errors: {len(console)}")
    for e in console[:5]:
        msg = e.get("text") or e.get("error") or ""
        print(f"    - {e.get('kind')}: {str(msg)[:160]}")

    if trusted_add and not submit_http:
        print("  >> INTERPRETATION: a real click reached the Add button, but NO network request fired.")
        print("     Likely cause: the contenteditable phone value never reaches Eitaa's internal model")
        print("     (input/change events not registered) -> fix by dispatching proper input events on fill.")
    elif trusted_add and submit_http:
        print("  >> INTERPRETATION: click fired AND request(s) went out; popup staying open likely means")
        print("     the server rejected it (e.g. number not registered on Eitaa) without a visible toast.")
    elif not trusted_add:
        print("  >> INTERPRETATION: no trusted click landed on the Add button; the click did not physically reach it.")


async def cmd_diagnose_add_contact(account: str, file: str) -> int:
    """Submit exactly one contact and capture UI/network evidence safely."""
    config.ensure_dirs()
    entries = _read_contacts_csv(file)
    if not entries:
        print("[diagnose-add-contact] no rows in", file)
        return 2

    entry = entries[0]
    phone = _normalize_ir_phone(entry["phone"])
    if phone is None:
        print("[diagnose-add-contact] first row has an invalid Iranian mobile number")
        return 2

    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[diagnose-add-contact] not logged in. run: python cli.py login --account", account)
            return 2

        try:
            await driver.open_contacts_view()
        except Exception as exc:  # noqa: BLE001
            print(f"[diagnose-add-contact] could not open Contacts: {exc}")
            return 1

        rec = RunRecorder(
            session,
            "diagnose_add_contact",
            ui_diagnostics=True,
            sensitive_literals=[
                entry.get("phone", ""), phone,
                entry.get("first", ""), entry.get("last", ""),
            ],
        )
        try:
            await rec.start()
        except Exception as exc:  # noqa: BLE001
            print(f"[diagnose-add-contact] instrumentation failed before submit: {type(exc).__name__}")
            return 1
        print(f"[diagnose-add-contact] run_id={rec.run_id}")
        print("[diagnose-add-contact] submitting only the first CSV row; sensitive values are not logged")
        await rec.baseline(seconds=1)

        holder: dict[str, dict] = {}

        async def submit_one() -> None:
            holder["result"] = await driver._add_one(
                phone,
                entry.get("first", ""),
                entry.get("last", ""),
                before_submit=lambda: rec.checkpoint("pre_submit"),
                after_submit=lambda: rec.checkpoint("post_submit"),
            )

        try:
            await rec.action(submit_one)
        except Exception as exc:  # noqa: BLE001
            holder["result"] = {
                "status": "error",
                "detail": f"diagnostic action failed: {type(exc).__name__}: {exc}",
            }
        finally:
            result = holder.get(
                "result",
                {"status": "error", "detail": "submission ended before a result was returned"},
            )
            run_dir = await rec.finish(
                extra_meta={
                    "ui_diagnostics": True,
                    "result_status": result.get("status", "error"),
                }
            )

        detail = rec.scrub_sensitive(str(result.get("detail", "")))
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        artifacts = meta.get("diagnostic_artifacts", {})
        missing = [name for name, present in artifacts.items() if not present]
        print(
            f"[diagnose-add-contact] result={result.get('status')} "
            f"detail={detail}"
        )
        print(f"[diagnose-add-contact] artifacts: {run_dir}")
        if meta.get("diagnostic_complete"):
            print("[diagnose-add-contact] evidence complete: pre/post submit PNG+DOM, click events, console, HTTP/WS metadata, trace")
        else:
            print(f"[diagnose-add-contact] evidence incomplete; missing={missing} ui_events={meta.get('event_counts', {}).get('ui', 0)}")
        _summarize_diagnostic_run(run_dir)
        return 0 if result.get("status") == "added" and meta.get("diagnostic_complete") else 1


async def cmd_send_file(
    account: str,
    to: str | None,
    saved: bool,
    file: str | None,
    caption: str,
    as_photo: bool = False,
) -> int:
    config.ensure_dirs()
    if not saved and not to:
        print("[send-file] specify --to <chat> or --saved")
        return 2

    # If no file is given, auto-create a small test file so the user can test
    # with zero setup.
    created_temp = False
    if not file:
        import tempfile
        fd, file = tempfile.mkstemp(suffix=".txt", prefix="mkwl_test_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("Mkwlsoso test file\n")
        created_temp = True
        if not caption:
            caption = "تست فایل از Mkwlsoso"
        print(f"[send-file] no --file given; created a test file: {file}")

    if not os.path.isfile(file):
        print(f"[send-file] file not found: {file}")
        return 2

    try:
        async with open_session(account) as session:
            driver = EitaaDriver(session)
            await driver.open()
            if not await driver.is_logged_in():
                print("[send-file] not logged in. run: python cli.py login --account", account)
                return 2
            target = "Saved Messages" if saved else to
            print(f"[send-file] sending {file!r} to {target!r}"
                  + (f" with caption {caption!r}" if caption else ""))
            res = await driver.send_file(
                file, caption=caption, query=to, to_saved=saved, as_photo=as_photo
            )
            print(f"[send-file] ok={res.ok} to={res.to!r} detail={res.detail}")
            return 0 if res.ok else 1
    finally:
        if created_temp:
            try:
                os.remove(file)
            except OSError:
                pass


async def cmd_stats(account: str) -> int:
    config.ensure_dirs()
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            print("[stats] not logged in. run: python cli.py login --account", account)
            return 2
        print("[stats] counting (this scrolls chats + contacts, takes a bit)...")
        stats = await driver.get_stats()
        c = stats["contacts"]
        c_str = "unknown (contacts view failed)" if c < 0 else str(c)
        print(f"[stats] account={account}")
        print(f"        contacts (مخاطبین): {c_str}")
        print(f"        private chats (پی‌وی): {stats['pvs']}")
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
    p_insp.add_argument("--add-contact", dest="add_contact", action="store_true",
                        help="open the add-contact popup and dump its form")
    p_insp.add_argument("--attach", action="store_true",
                        help="open Saved Messages and reveal the file-upload UI (safe)")
    p_insp.add_argument("--attach-file", dest="attach_file", default=None,
                        help="optional file to attach so the caption box + send button are revealed (not sent)")
    p_insp.add_argument("--login", action="store_true",
                        help="dump the auth/login page structure (run on a FRESH, not-logged-in profile)")

    p_chats = sub.add_parser("chats", help="list visible chat titles (choose one for --to)")
    p_chats.add_argument("--account", required=True)

    p_contacts = sub.add_parser("contacts", help="collect ALL contacts from the Contacts view")
    p_contacts.add_argument("--account", required=True)
    p_contacts.add_argument("--out", default="contacts.txt", help="output file path")

    p_stats = sub.add_parser("stats", help="show account stats (contacts count + PV count)")
    p_stats.add_argument("--account", required=True)

    p_addc = sub.add_parser("add-contacts", help="create contacts from a CSV (phone,first_name,last_name)")
    p_addc.add_argument("--account", required=True)
    p_addc.add_argument("--file", required=True, help="CSV file: phone,first_name,last_name")
    p_addc.add_argument("--limit", type=int, default=None, help="only process the first N rows (safe test)")

    p_diag_add = sub.add_parser(
        "diagnose-add-contact",
        help="submit the first CSV row and capture exact UI/network diagnostics",
    )
    p_diag_add.add_argument("--account", required=True)
    p_diag_add.add_argument("--file", required=True, help="CSV file; only the first row is used")

    p_send = sub.add_parser("send", help="send one text message via the browser driver")
    p_send.add_argument("--account", required=True)
    p_send.add_argument("--to", required=True, help="chat/contact name or username to open")
    p_send.add_argument("--text", required=True, help="message text to send")
    p_send.add_argument("--no-verify", action="store_true", help="skip DOM verification")

    p_sfile = sub.add_parser("send-file", help="upload and send a file (optionally with a caption)")
    p_sfile.add_argument("--account", required=True)
    p_sfile.add_argument("--to", default=None, help="chat/contact name to send to")
    p_sfile.add_argument("--saved", action="store_true", help="send to your own Saved Messages (safe test)")
    p_sfile.add_argument("--file", default=None, help="file to send; if omitted a small test .txt is created")
    p_sfile.add_argument("--caption", default="", help="optional caption text")
    p_sfile.add_argument("--as-photo", dest="as_photo", action="store_true",
                        help="send as photo/video (عکس یا ویدیو) instead of a document (فایل)")

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
        return asyncio.run(cmd_inspect(
            args.account, args.open, args.menu, args.add_contact,
            args.attach, args.attach_file, args.login,
        ))
    if args.command == "chats":
        return asyncio.run(cmd_chats(args.account))
    if args.command == "contacts":
        return asyncio.run(cmd_contacts(args.account, args.out))
    if args.command == "stats":
        return asyncio.run(cmd_stats(args.account))
    if args.command == "add-contacts":
        return asyncio.run(cmd_add_contacts(args.account, args.file, args.limit))
    if args.command == "diagnose-add-contact":
        return asyncio.run(cmd_diagnose_add_contact(args.account, args.file))
    if args.command == "send":
        return asyncio.run(cmd_send(args.account, args.to, args.text, args.no_verify))
    if args.command == "send-file":
        return asyncio.run(cmd_send_file(
            args.account, args.to, args.saved, args.file, args.caption, args.as_photo
        ))
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
