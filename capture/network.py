"""HTTP capture.

Listens to Playwright request/response events and records a redacted summary of
each exchange. Bodies are summarized through the redactor, never stored raw.

Every event carries a monotonic timestamp, wall-clock time, and a sequence
number so the analyzer can order and correlate events across sources.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from playwright.async_api import Page, Request, Response

from capture import redactor


class HttpRecorder:
    def __init__(self, emit: Callable[[dict[str, Any]], None], seq: Callable[[], int]) -> None:
        self._emit = emit
        self._seq = seq
        self._page: Page | None = None

    def attach(self, page: Page) -> None:
        self._page = page
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)

    def detach(self) -> None:
        if self._page is None:
            return
        self._page.remove_listener("request", self._on_request)
        self._page.remove_listener("response", self._on_response)
        self._page.remove_listener("requestfailed", self._on_request_failed)
        self._page = None

    def _base(self, kind: str) -> dict[str, Any]:
        return {
            "source": "http",
            "kind": kind,
            "seq": self._seq(),
            "mono": time.monotonic(),
            "ts": time.time(),
        }

    def _on_request(self, request: Request) -> None:
        try:
            post = request.post_data_buffer
        except Exception:  # noqa: BLE001 - Playwright may not expose it
            post = None
        evt = self._base("request")
        evt.update(
            {
                "method": request.method,
                "url": redactor.scrub_text(request.url),
                "resource_type": request.resource_type,
                "headers": redactor.redact_headers(request.headers),
                "body": redactor.summarize_body(
                    post, request.headers.get("content-type")
                ),
            }
        )
        self._emit(evt)

    def _on_response(self, response: Response) -> None:
        evt = self._base("response")
        evt.update(
            {
                "status_code": response.status,
                "url": redactor.scrub_text(response.url),
                "headers": redactor.redact_headers(response.headers),
            }
        )
        self._emit(evt)

    def _on_request_failed(self, request: Request) -> None:
        evt = self._base("requestfailed")
        evt.update(
            {
                "method": request.method,
                "url": redactor.scrub_text(request.url),
                "failure": request.failure,
            }
        )
        self._emit(evt)
