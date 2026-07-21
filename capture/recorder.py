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
    def __init__(
        self,
        session: BrowserSession,
        op: str,
        ui_diagnostics: bool = False,
        sensitive_literals: list[str] | None = None,
    ) -> None:
        self.session = session
        self.op = op
        self.run_id = new_run_id(session.account, op)
        self.run_dir: Path = config.ARTIFACTS_DIR / self.run_id
        self.marker = f"MKWL_CAPTURE_{self.run_id}"
        self.ui_diagnostics = ui_diagnostics
        self._sensitive_literals = sorted(
            {value for value in (sensitive_literals or []) if value},
            key=len,
            reverse=True,
        )

        self._seq = itertools.count(1)
        self._phase = "init"
        self._files: dict[str, Any] = {}
        self._counts: dict[str, int] = {}

        # Diagnostic captures store request metadata only: size/hash/type, not
        # the contact form's request body.
        self._http = HttpRecorder(self._emit, self._next_seq, metadata_only=ui_diagnostics)
        self._ws = WebSocketRecorder(self._emit, self._next_seq, metadata_only=ui_diagnostics)

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

    def scrub_sensitive(self, value: Any) -> Any:
        """Redact run-specific values such as the submitted name/phone."""
        if isinstance(value, str):
            text = redactor.scrub_text(value)
            for literal in self._sensitive_literals:
                text = text.replace(literal, "<SENSITIVE>")
            return text
        if isinstance(value, dict):
            return {key: self.scrub_sensitive(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.scrub_sensitive(item) for item in value]
        if isinstance(value, tuple):
            return [self.scrub_sensitive(item) for item in value]
        return value

    def _emit(self, event: dict[str, Any]) -> None:
        event = self.scrub_sensitive(event)
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

    # ---- optional UI diagnostics ----------------------------------------
    def _diagnostic_event(self, source: str, kind: str, **fields: Any) -> None:
        event = {
            "source": source,
            "kind": kind,
            "seq": self._next_seq(),
            "mono": time.monotonic(),
            "ts": time.time(),
        }
        event.update(redactor.redact_value(fields))
        self._emit(event)

    def _on_console(self, message: Any) -> None:
        if message.type not in {"warning", "error"}:
            return
        self._diagnostic_event(
            "console",
            message.type,
            text=redactor.scrub_text(message.text)[:2000],
        )

    def _on_page_error(self, error: Any) -> None:
        self._diagnostic_event(
            "console",
            "pageerror",
            error=redactor.scrub_text(str(error))[:3000],
        )

    def _on_ui_event(self, event: dict[str, Any]) -> None:
        self._diagnostic_event("ui", str(event.pop("kind", "event")), **event)

    async def _install_ui_diagnostics(self) -> None:
        assert self.session.page is not None
        page = self.session.page
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        await page.expose_function("__mkwl_emit_ui_event", self._on_ui_event)
        await page.evaluate(
            """
            () => {
              if (window.__MKWL_UI_DIAGNOSTICS) return;
              window.__MKWL_UI_DIAGNOSTICS = true;
              const describe = (target) => {
                const el = target && target.nodeType === 1 ? target : target && target.parentElement;
                if (!el) return {};
                const r = el.getBoundingClientRect();
                const action = el.closest('button, [role="button"]');
                const actionShape = action ? {
                  tag: action.tagName,
                  id: (action.id || '').slice(0, 80),
                  cls: String(action.className || '').slice(0, 240),
                  role: action.getAttribute('role'),
                  aria: (action.getAttribute('aria-label') || '').slice(0, 160),
                  text: (action.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80)
                } : null;
                return {
                  tag: el.tagName,
                  id: (el.id || '').slice(0, 80),
                  cls: String(el.className || '').slice(0, 240),
                  role: el.getAttribute('role'),
                  aria: (el.getAttribute('aria-label') || '').slice(0, 160),
                  disabled: el.disabled === true || el.getAttribute('disabled') !== null,
                  action: actionShape,
                  box: {
                    x: Math.round(r.x), y: Math.round(r.y),
                    width: Math.round(r.width), height: Math.round(r.height)
                  }
                };
              };
              for (const kind of ['pointerdown', 'mousedown', 'mouseup', 'pointerup', 'click']) {
                document.addEventListener(kind, (event) => {
                  try {
                    window.__mkwl_emit_ui_event({
                      kind, trusted: event.isTrusted, button: event.button,
                      detail: event.detail, target: describe(event.target)
                    });
                  } catch (_) {}
                }, true);
              }
            }
            """
        )
        installed = await page.evaluate(
            "() => window.__MKWL_UI_DIAGNOSTICS === true "
            "&& typeof window.__mkwl_emit_ui_event === 'function'"
        )
        if not installed:
            raise RuntimeError("UI diagnostic event hook did not install")

    async def _dom_snapshot(self, tag: str) -> None:
        if not self.ui_diagnostics:
            return
        assert self.session.page is not None
        try:
            snapshot = await self.session.page.evaluate(
                """
                () => {
                  const visible = (n) => !!(n && n.getClientRects().length);
                  const shape = (n) => ({
                    tag: n.tagName,
                    id: (n.id || '').slice(0, 80),
                    cls: String(n.className || '').slice(0, 240),
                    role: n.getAttribute('role'),
                    aria: (n.getAttribute('aria-label') || '').slice(0, 160),
                    disabled: n.disabled === true || n.getAttribute('disabled') !== null,
                    editable: n.getAttribute('contenteditable') === 'true',
                  });
                  const popup = document.querySelector('.popup.active');
                  const fields = popup ? Array.from(popup.querySelectorAll(
                    'input, textarea, [contenteditable="true"], .input-field-input'
                  )).filter(visible).slice(0, 20).map(n => ({
                    ...shape(n),
                    chars: String(n.value || n.textContent || '').length
                  })) : [];
                  const chrome = popup ? Array.from(popup.querySelectorAll(
                    'label, button, .error, .input-field-input-error, .popup-description, [role="alert"]'
                  )).filter(visible).slice(0, 40).map(n => ({
                    ...shape(n),
                    text: (n.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 300)
                  })) : [];
                  const toasts = Array.from(document.querySelectorAll(
                    '.toast, .toast-body, [class*=toast], [role="alert"]'
                  )).filter(visible).slice(0, 20).map(n => ({
                    ...shape(n),
                    text: (n.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 500)
                  }));
                  return {
                    url: location.href,
                    ready_state: document.readyState,
                    body_class: String(document.body && document.body.className || '').slice(0, 240),
                    popup: popup ? shape(popup) : null,
                    fields, chrome, toasts
                  };
                }
                """
            )
            safe = self.scrub_sensitive(redactor.redact_value(snapshot))
            (self.run_dir / f"{tag}_dom.json").write_text(
                json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            self._diagnostic_event("console", "dom_snapshot_failed", error=str(exc))
            raise

    # ---- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        assert self.session.page is not None
        self._http.attach(self.session.page)
        self._ws.attach(self.session.page)
        if self.ui_diagnostics:
            await self._install_ui_diagnostics()
        try:
            await self.session.context.tracing.start(  # type: ignore[union-attr]
                screenshots=True, snapshots=True, sources=False
            )
        except Exception:  # noqa: BLE001 - normal captures remain best-effort
            if self.ui_diagnostics:
                raise

    async def baseline(self, seconds: int | None = None) -> None:
        self._phase = "baseline"
        await asyncio.sleep(seconds if seconds is not None else config.BASELINE_SECONDS)

    async def checkpoint(self, tag: str) -> None:
        """Capture an immediate diagnostic screenshot and safe DOM snapshot."""
        if not self.ui_diagnostics:
            return
        await self._dom_snapshot(tag)
        await self._screenshot(tag)

    async def action(self, do_action: Callable[[], Awaitable[None]] | None) -> None:
        self._phase = "action"
        await self._dom_snapshot("before")
        await self._screenshot("before")
        if do_action is not None:
            await do_action()
        self._phase = "trail"
        await asyncio.sleep(config.ACTION_TRAIL_SECONDS)
        await self._screenshot("after")
        await self._dom_snapshot("after")

    async def _screenshot(self, tag: str) -> None:
        try:
            assert self.session.page is not None
            masks = None
            if self.ui_diagnostics:
                masks = [
                    self.session.page.locator(".popup.active .input-field-input"),
                    self.session.page.locator(".peer-title"),
                ]
            await self.session.page.screenshot(
                path=str(self.run_dir / f"{tag}.png"),
                mask=masks,
                mask_color="#777777",
            )
        except Exception:  # noqa: BLE001
            if self.ui_diagnostics:
                raise

    async def finish(self, extra_meta: dict[str, Any] | None = None) -> Path:
        try:
            await self.session.context.tracing.stop(  # type: ignore[union-attr]
                path=str(self.run_dir / "trace.zip")
            )
        except Exception:  # noqa: BLE001
            pass
        if self.ui_diagnostics and self.session.page is not None:
            self.session.page.remove_listener("console", self._on_console)
            self.session.page.remove_listener("pageerror", self._on_page_error)
        self._http.detach()
        self._ws.detach()

        diagnostic_artifacts: dict[str, bool] | None = None
        if self.ui_diagnostics:
            expected = [
                "before.png", "before_dom.json",
                "pre_submit.png", "pre_submit_dom.json",
                "post_submit.png", "post_submit_dom.json",
                "after.png", "after_dom.json",
                "events.jsonl", "trace.zip",
            ]
            diagnostic_artifacts = {
                name: (self.run_dir / name).exists() for name in expected
            }

        meta = {
            "run_id": self.run_id,
            "account": self.session.account,
            "op": self.op,
            "marker": self.marker,
            "url": config.EITAA_WEB_URL,
            "created": time.time(),
            "event_counts": self._counts,
        }
        if diagnostic_artifacts is not None:
            meta["diagnostic_artifacts"] = diagnostic_artifacts
            meta["diagnostic_complete"] = (
                all(diagnostic_artifacts.values()) and self._counts.get("ui", 0) > 0
            )
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
