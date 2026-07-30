"""Read-through cache for the small JSON stores, keyed by file identity.

Drawing one panel screen was doing this, per account:

    contacts_store.count()        -> parse the WHOLE contacts file (549 entries)
    contacts_store.age_hours()    -> parse it AGAIN
    blocked_store.count()         -> parse another file
    progress_store.done_count()   -> parse another file
    direct_ctx.has_context()      -> parse a capture AND scan its bytes

With several accounts that is dozens of full JSON parses for a handful of
integers, on a host with one CPU core of which 30-89% is stolen. This module
parses a file once and returns the SAME object until the file actually changes
(mtime + size + inode), so a redraw costs almost nothing.

Writes go through here too, which is what keeps the cache honest: every writer
updates the entry it just wrote. Files are written COMPACT - the stores used
`indent=2`, roughly tripling both file size and parse time for no benefit.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

_LOCK = threading.Lock()
#: path -> (identity, value)
_CACHE: dict[str, tuple[tuple, Any]] = {}
_STATS = {"hits": 0, "misses": 0, "writes": 0, "invalidations": 0}


def _identity(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (int(st.st_mtime_ns), st.st_size, st.st_ino)


def stats() -> dict:
    """Cache counters (used by the timing card to prove this is working)."""
    with _LOCK:
        return dict(_STATS, cached_files=len(_CACHE))


def load_json(path: Path, default_factory: Callable[[], Any]) -> Any:
    """Parsed contents of `path`, reused while the file is unchanged.

    The caller must treat the result as READ-ONLY: it is shared. Every store in
    this project already copies what it mutates.
    """
    key = str(path)
    ident = _identity(path)
    if ident is None:
        return default_factory()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == ident:
            _STATS["hits"] += 1
            return hit[1]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt file behaves as "missing"
        value = default_factory()
    with _LOCK:
        _STATS["misses"] += 1
        # Re-stat: if the file changed while we parsed it, do not cache a value
        # that never existed on disk.
        if _identity(path) == ident:
            _CACHE[key] = (ident, value)
    return value


def write_json(path: Path, value: Any) -> None:
    """Atomically write `value` COMPACT, and refresh the cache entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)
    with _LOCK:
        _STATS["writes"] += 1
        ident = _identity(path)
        if ident is not None:
            _CACHE[str(path)] = (ident, value)


def invalidate(path: Path | None = None) -> None:
    with _LOCK:
        _STATS["invalidations"] += 1
        if path is None:
            _CACHE.clear()
        else:
            _CACHE.pop(str(path), None)
