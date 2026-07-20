"""Run recorder.

Orchestrates a single capture run:

  1. idle baseline  -> record background traffic with no user action
  2. action window  -> the owner (or an automation) performs ONE operation
  3. trail          -> keep recording briefly to catch async replies

All events from the HTTP and WebSocket recorders are streamed to JSONL files
under artifacts/<run_id>/. Screenshots and a Playwright trace bracket the
action so it can be reviewed visually.

The recorder owns the single global sequence counter shared by all sources so
events can be totally ordered during analysis.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from config import config
from capture import redactor
from capture.browser import BrowserSession
from capture.network import HttpRecorder
from capture.websocket import WebSocketRecorder


def new_run_id(account: str, op: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{account}_{op}"


class RunRecorder:
    def __init__(self, session: BrowserSession, op: str) -> None:
        self.session = session
        self.op = op
        self.run_id = new_run_id(session.account, op)
        self.run_dir: Path = config.ARTIFACTS_DIR / self.run_id
        self.marker = f"MKWL_CAPTURE_{self.run_id}"

        self._seq = itertools.count(1)
        self._phase = "init"
        self._files: dict[str, Any] = {}
        self._counts: dict[str, int] = {}

        self._http = HttpRecorder(self._emit, self._next_seq)
        self._ws = WebSocketRecorder(self._emit, self._next_seq)

    # ---- infrastructure -------------------------------------------------
    def _next_seq(self) -> int:
        return next(self._seq)

    def _stream(self, name: str):
        if name not in self._files:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self._files[name] = open(self.run_dir / f"{name}.jsonl", "a", encoding="utf-8")
        return self._files[name]

    def emit_event(self, event: dict[str, Any]) -> None:
        """Public entry so deep-capture helpers can stream events into the run."""
        self._emit(event)

    def _emit(self, event: dict[str, Any]) -> None:
        event["phase"] = self._phase
        source = event.get("source", "misc")
        # events.jsonl gets everything; per-source files help analysis.
        line = json.dumps(event, ensure_ascii=False)
        self._stream("events").write(line + "\n")
        self._stream(source).write(line + "\n")
        self._counts[source] = self._counts.get(source, 0) + 1

    def _flush(self) -> None:
        for fh in self._files.values():
            try:
                fh.flush()
            except Exception:  # noqa: BLE001
                pass

    # ---- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        assert self.session.page is not None
        self._http.attach(self.session.page)
        self._ws.attach(self.session.page)
        try:
            await self.session.context.tracing.start(  # type: ignore[union-attr]
                screenshots=True, snapshots=True, sources=False
            )
        except Exception:  # noqa: BLE001 - tracing is best-effort
            pass

    async def baseline(self, seconds: int | None = None) -> None:
        self._phase = "baseline"
        await asyncio.sleep(seconds if seconds is not None else config.BASELINE_SECONDS)

    async def action(self, do_action: Callable[[], Awaitable[None]] | None) -> None:
        self._phase = "action"
        await self._screenshot("before")
        if do_action is not None:
            await do_action()
        self._phase = "trail"
        await asyncio.sleep(config.ACTION_TRAIL_SECONDS)
        await self._screenshot("after")

    async def _screenshot(self, tag: str) -> None:
        try:
            assert self.session.page is not None
            await self.session.page.screenshot(path=str(self.run_dir / f"{tag}.png"))
        except Exception:  # noqa: BLE001
            pass

    async def finish(self, extra_meta: dict[str, Any] | None = None) -> Path:
        try:
            await self.session.context.tracing.stop(  # type: ignore[union-attr]
                path=str(self.run_dir / "trace.zip")
            )
        except Exception:  # noqa: BLE001
            pass
        self._http.detach()
        self._ws.detach()

        meta = {
            "run_id": self.run_id,
            "account": self.session.account,
            "op": self.op,
            "marker": self.marker,
            "url": config.EITAA_WEB_URL,
            "created": time.time(),
            "event_counts": self._counts,
        }
        if extra_meta:
            meta.update(extra_meta)
        (self.run_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        self._flush()
        for fh in self._files.values():
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        self._files.clear()
        return self.run_dir
