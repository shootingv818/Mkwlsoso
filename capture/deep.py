"""Deep protocol capture helpers.

These sit on top of a live BrowserSession and pull the extra evidence needed to
reverse Eitaa's transport/crypto:

- pull_hooks(): drain the in-page instrumentation buffer (window.__MKWL_dump)
- download_assets(): fetch all loaded JS/WASM through the authenticated context
- dump_storage(): record IndexedDB db names + localStorage KEY NAMES only

Raw request/response bytes captured by the hooks are small (Eitaa frames are a
few hundred bytes) and land in the run's gitignored artifacts. They are the
owner's own encrypted frames, kept locally for offline analysis.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import BrowserContext, Page

HOOKS_JS = Path(__file__).with_name("hooks.js")


async def pull_hooks(page: Page, emit: Callable[[dict[str, Any]], None]) -> int:
    """Drain buffered hook records and emit each as a `source=hook` event."""
    try:
        records = await page.evaluate(
            "() => (window.__MKWL_dump ? window.__MKWL_dump() : [])"
        )
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for rec in records or []:
        evt = {"source": "hook", "kind": rec.get("k", "?")}
        evt.update(rec)
        emit(evt)
        n += 1
    return n


async def collect_asset_urls(page: Page) -> list[str]:
    """List JS/WASM resource URLs the page actually loaded."""
    js = """
    () => performance.getEntriesByType('resource')
      .map(e => e.name)
      .filter(u => /\\.(js|wasm|mjs)(\\?|$)/i.test(u))
    """
    try:
        urls = await page.evaluate(js)
    except Exception:  # noqa: BLE001
        return []
    # Deduplicate, keep order.
    return list(dict.fromkeys(urls or []))


async def download_assets(context: BrowserContext, urls: list[str], out_dir: Path) -> dict[str, Any]:
    """Download each asset through the authenticated context and save + hash."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for url in urls:
        entry: dict[str, Any] = {"url": url}
        try:
            resp = await context.request.get(url, timeout=30000)
            body = await resp.body()
            digest = hashlib.sha256(body).hexdigest()
            name = _safe_name(url, digest)
            (out_dir / name).write_bytes(body)
            entry.update({"status": resp.status, "size": len(body), "sha256": digest, "file": name})
        except Exception as exc:  # noqa: BLE001
            entry.update({"error": str(exc)})
        manifest.append(entry)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"count": len(manifest), "dir": str(out_dir)}


def _safe_name(url: str, digest: str) -> str:
    tail = url.split("?")[0].rstrip("/").split("/")[-1] or "asset"
    tail = "".join(c if c.isalnum() or c in "._-" else "_" for c in tail)[-60:]
    return f"{digest[:12]}_{tail}"


async def dump_storage(page: Page) -> dict[str, Any]:
    """Structural snapshot only: IndexedDB db names + localStorage key names.

    Values are NOT read here (they may hold session secrets). We only record
    the shape so we know WHERE the session/auth state lives.
    """
    js = """
    async () => {
      const out = { localStorage_keys: [], indexeddb: [] };
      try { out.localStorage_keys = Object.keys(localStorage || {}); } catch (e) {}
      try {
        if (indexedDB.databases) {
          const dbs = await indexedDB.databases();
          out.indexeddb = dbs.map(d => ({ name: d.name, version: d.version }));
        }
      } catch (e) {}
      return out;
    }
    """
    try:
        return await page.evaluate(js)
    except Exception:  # noqa: BLE001
        return {"localStorage_keys": [], "indexeddb": []}
