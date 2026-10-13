"""WebSocket capture.

Eitaa web (like many messengers) may carry real-time traffic over WebSocket.
We record connection open/close and every frame's direction, type, size, and a
stable hash. Frame payloads are summarized via the redactor; raw text/binary is
never written in clear.

Each socket gets a connection id; each frame gets a per-connection sequence so
reconnects and duplicate frames can be told apart during analysis.
"""

from __future__ import annotations

import hashlib
import itertools
import time
from typing import Any, Callable

from playwright.async_api import Page, WebSocket

from capture import redactor


class WebSocketRecorder:
    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        seq: Callable[[], int],
        metadata_only: bool = False,
    ) -> None:
        self._emit = emit
        self._seq = seq
        self._metadata_only = metadata_only
        self._page: Page | None = None
        self._conn_ids = itertools.count(1)

    def attach(self, page: Page) -> None:
        self._page = page
        page.on("websocket", self._on_ws)

    def detach(self) -> None:
        if self._page is not None:
            self._page.remove_listener("websocket", self._on_ws)
            self._page = None

    def _base(self, kind: str, conn_id: int) -> dict[str, Any]:
        return {
            "source": "ws",
            "kind": kind,
            "conn_id": conn_id,
            "seq": self._seq(),
            "mono": time.monotonic(),
            "ts": time.time(),
        }

    def _body_summary(self, payload: bytes | str, content_type: str) -> dict[str, Any]:
        if not self._metadata_only:
            return redactor.summarize_body(payload, content_type)
        data = payload if isinstance(payload, bytes) else payload.encode("utf-8", "replace")
        return {
            "present": True,
            "kind": "metadata-only",
            "size": len(data),
            "sha256_16": hashlib.sha256(data).hexdigest()[:16],
            "content_type": content_type,
        }

    def _on_ws(self, ws: WebSocket) -> None:
        conn_id = next(self._conn_ids)
        frame_seq = itertools.count(1)

        open_evt = self._base("ws_open", conn_id)
        open_evt["url"] = redactor.scrub_text(ws.url)
        self._emit(open_evt)

        def _frame(direction: str, payload: Any) -> None:
            is_bytes = isinstance(payload, (bytes, bytearray))
            evt = self._base("ws_frame", conn_id)
            evt.update(
                {
                    "direction": direction,
                    "frame_seq": next(frame_seq),
                    "opcode": "binary" if is_bytes else "text",
                    "body": self._body_summary(
                        bytes(payload) if is_bytes else payload,
                        "application/octet-stream" if is_bytes else "text/plain",
                    ),
                }
            )
            self._emit(evt)

        ws.on("framesent", lambda p: _frame("sent", p))
        ws.on("framereceived", lambda p: _frame("received", p))
        ws.on("socketerror", lambda err: self._emit({**self._base("ws_error", conn_id), "error": redactor.scrub_text(str(err))}))
        ws.on("close", lambda _ws: self._emit(self._base("ws_close", conn_id)))
