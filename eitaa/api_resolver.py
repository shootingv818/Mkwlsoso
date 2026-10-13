"""Inject eitaa/api_resolver.js into a page, once.

Every bridge in this repo needs the same thing: the object that owns
`invokeApi`. Eitaa moved it (see api_resolver.js for the evidence), so the
lookup now lives in one place instead of being hardcoded as `window.apiManager`
in seven files.

This module is deliberately tiny and dependency-free: it reads the JS once at
import and exposes one idempotent injector. It never raises into a job - a
caller that cannot inject simply gets False and keeps its existing fallback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_PATH = Path(__file__).with_name("api_resolver.js")
try:
    RESOLVER_SRC = _PATH.read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    RESOLVER_SRC = ""

# How long to wait for the resolver to find an api object. The old login code
# waited 30 x 500 ms = 15 s for `window.apiManager`; on this host the app needs
# roughly 10-12 s after domcontentloaded before the managers exist, which left
# almost no margin. Raised, and tunable, because a slow host should wait rather
# than report a missing feature.
READY_TIMEOUT = float(os.environ.get("MKWL_API_READY_TIMEOUT", "60"))

# The in-page readiness expression, with a fallback for the case where the
# resolver itself could not be injected (then it behaves like the old check).
READY_JS = (
    "() => (window.__MKWL_apiReady ? window.__MKWL_apiReady() "
    ": !!(window.apiManager && window.apiManager.invokeApi))"
)


async def ensure_api_resolver(page: Any) -> bool:
    """Define window.__MKWL_api (+ helpers) in the page. Idempotent.

    Returns False only when the JS could not be injected at all; it does NOT
    mean an api object exists yet - ask `api_ready()` for that.
    """
    try:
        if await page.evaluate("() => typeof window.__MKWL_api === 'function'"):
            return True
    except Exception:  # noqa: BLE001
        return False
    if not RESOLVER_SRC:
        return False
    try:
        await page.evaluate(RESOLVER_SRC)
        return await page.evaluate("() => typeof window.__MKWL_api === 'function'")
    except Exception:  # noqa: BLE001
        return False


async def api_ready(page: Any) -> bool:
    """True once something exposing invokeApi can be resolved."""
    try:
        return bool(await page.evaluate(READY_JS))
    except Exception:  # noqa: BLE001
        return False


async def api_info(page: Any) -> dict:
    """Which route was taken, for error cards and diagnostics. Never raises."""
    try:
        info = await page.evaluate(
            "() => (window.__MKWL_apiInfo ? window.__MKWL_apiInfo() : null)")
        return info if isinstance(info, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
