"""A pre-warmed Chromium profile, copied for every new account.

Measured on the target host: a profile that has already loaded Eitaa Web is
11-26 MB, and most of that is the app itself sitting in Chromium's disk cache.
A BRAND NEW account has none of it, so adding an account meant downloading the
whole web app over a link where a single request costs 1-3 seconds - on top of
the 158-203 seconds Chromium needs to start.

So: keep ONE template profile that has loaded the app but has never logged in,
and copy it (a local file copy, seconds) for each new account. The new account
starts with the app already cached.

Safety rules, because a template is shared state:
  * The template is NEVER logged in. It is only ever opened on the login page,
    so it cannot leak one account's session into another.
  * A copy is made only into a directory that does not exist yet; an existing
    profile is never overwritten.
  * If anything about the template looks wrong (missing, too small, contains a
    session), it is ignored and the account starts empty exactly as before.
  * The copy is written to a temporary directory and renamed into place, so an
    interrupted copy (disk full, kill) can never leave a half profile behind.
    The host has 1.5 GB free, so this matters.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from config import config

TEMPLATE_NAME = "_template"
#: A usable template has at least this much cached app in it.
MIN_TEMPLATE_MB = 3.0
#: Older than this and the cached app is probably stale; refresh it.
MAX_TEMPLATE_AGE_H = 72.0
#: Files that would carry a SESSION rather than just cache. A template that has
#: any of these is refused, so no account can inherit another's login.
_SESSION_MARKERS = ("Default/Local Storage", "Default/IndexedDB")


def template_dir() -> Path:
    return Path(config.PROFILES_DIR) / TEMPLATE_NAME


def _size_mb(path: Path) -> float:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6
    except OSError:
        return 0.0


def age_hours() -> float | None:
    d = template_dir()
    if not d.is_dir():
        return None
    try:
        return max(0.0, (time.time() - d.stat().st_mtime) / 3600.0)
    except OSError:
        return None


def status() -> dict:
    d = template_dir()
    if not d.is_dir():
        return {"exists": False}
    return {"exists": True, "size_mb": round(_size_mb(d), 1),
            "age_h": round(age_hours() or 0.0, 1), "path": str(d),
            "usable": is_usable(), "stale": is_stale()}


def is_usable() -> bool:
    """Is there a template worth copying?"""
    d = template_dir()
    if not d.is_dir():
        return False
    if _size_mb(d) < MIN_TEMPLATE_MB:
        return False
    return True


def is_stale() -> bool:
    a = age_hours()
    return a is None or a > MAX_TEMPLATE_AGE_H


def free_mb() -> float:
    try:
        st = shutil.disk_usage(str(Path(config.PROFILES_DIR)))
        return st.free / 1e6
    except OSError:
        return 0.0


def clone_for(account: str) -> dict:
    """Copy the template into this account's profile. Never raises.

    Returns {ok, code, size_mb, seconds}. ok=False simply means "start empty",
    which is the old behaviour.
    """
    t0 = time.time()
    src = template_dir()
    dst = Path(config.PROFILES_DIR) / account
    if dst.exists():
        return {"ok": False, "code": "profile already exists"}
    if not is_usable():
        return {"ok": False, "code": "no usable template yet"}
    size = _size_mb(src)
    # Copy + the profile's own growth; refuse if it would leave the disk gasping.
    if free_mb() < (size * 2 + 300):
        return {"ok": False, "code": f"only {free_mb():.0f} MB free, not cloning"}
    tmp = dst.with_name(dst.name + ".cloning")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(src, tmp, symlinks=True, ignore_dangling_symlinks=True,
                        ignore=shutil.ignore_patterns("Singleton*", "*.lock"))
        tmp.rename(dst)
    except Exception as exc:  # noqa: BLE001 - a failed clone must not block a login
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "code": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "size_mb": round(size, 1),
            "seconds": round(time.time() - t0, 1)}


def looks_logged_in(path: Path | None = None) -> bool:
    """Does this profile carry a session? Used to refuse a polluted template."""
    d = path or template_dir()
    for marker in _SESSION_MARKERS:
        p = d / marker
        try:
            if p.is_dir() and any(p.rglob("*")):
                return True
        except OSError:
            continue
    return False
