"""Isolated APK send-mode policy.

WHY THIS EXISTS
---------------
Eitaa refuses a document whose MIME is the real apk type
``application/vnd.android.package-archive`` — the send fails. A working client
observed live uploads the apk as a GENERIC binary (``application/octet-stream``)
and lets the real ``.apk`` name ride in the ``documentAttributeFilename``
attribute, so the recipient still receives a valid apk while Eitaa's apk-MIME
filter never triggers.

This module is the single, opt-in place that applies that behaviour. It is
DELIBERATELY tiny, dependency-free (stdlib only) and defensive: importing it or
calling any function here must NEVER be able to break a normal send. Every entry
point falls back to the caller's original MIME on any error, and the whole
feature is OFF unless the owner turns it on in the Settings panel.

Toggle: env var ``MKWL_APK_OCTET`` (read live, so the panel toggle takes effect
without a restart). The Settings store owns persistence and calls ``set_env``.

Safe to delete: remove this file and the two ``effective_mime`` call sites and
the project behaves exactly as before.
"""

from __future__ import annotations

import os

#: Environment flag the panel toggles. Read live on every send.
APK_OCTET_ENV = "MKWL_APK_OCTET"

#: The MIME used to smuggle an apk past Eitaa's type filter.
GENERIC_BINARY = "application/octet-stream"

#: The real apk MIME that Eitaa refuses (kept here only for reference/tests).
BLOCKED_APK_MIME = "application/vnd.android.package-archive"

_TRUE = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """True when APK send-mode is on. Never raises."""
    try:
        return str(os.environ.get(APK_OCTET_ENV, "")).strip().lower() in _TRUE
    except Exception:  # noqa: BLE001 - a flag read must never break a send
        return False


def set_env(value: bool) -> None:
    """Reflect the persisted setting into the live environment. Never raises."""
    try:
        os.environ[APK_OCTET_ENV] = "1" if value else "0"
    except Exception:  # noqa: BLE001
        pass


def is_apk(file_name: str) -> bool:
    """True only for a real ``.apk`` filename. Never raises."""
    try:
        name = str(file_name)
        return "." in name and name.rsplit(".", 1)[-1].lower() == "apk"
    except Exception:  # noqa: BLE001
        return False


def effective_mime(file_name: str, base_mime: str) -> str:
    """The MIME to actually put on the wire for ``file_name``.

    When APK mode is ON and the file is an ``.apk``, return the generic binary
    type so Eitaa does not block it. In every other case (mode off, not an apk,
    or ANY error) return ``base_mime`` unchanged, so a non-apk send and the
    mode-off path are byte-for-byte identical to before this module existed.
    """
    try:
        if is_apk(file_name) and enabled():
            return GENERIC_BINARY
    except Exception:  # noqa: BLE001 - fall back to the caller's MIME on anything
        pass
    return base_mime
