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


# ===========================================================================
# APK ZIP-WRAP mode: the real fix (supersedes the octet trick above).
#
# Eitaa's filter does not merely check the MIME; it inspects the archive and
# drops a zip whose entries it can enumerate when one is an apk. A raw .apk (any
# MIME) is dropped on this build. The working evasion, reverse-engineered from a
# third-party bot's actually-delivered file ('عکسایی-دوستی.zip' carrying inner
# 'یادگاری.apk'), is to wrap the apk in a deliberately-malformed-but-Android-
# valid zip so Eitaa's parser enumerates ZERO entries (finds no apk, delivers)
# while Android / Info-ZIP read the one real entry and install it. The inner
# entry KEEPS its .apk name, so an elderly recipient just opens the zip and taps
# the apk - one tap, no rename.
#
# Structure, byte-matched to the working file:
#   * one real entry '<name>.apk', method 8 (deflate)
#   * general-purpose bit 3 (0x0008) = streaming data descriptor; the LOCAL
#     header carries crc=0 and compressed_size=0, with the true crc/sizes in a
#     trailing PK\x07\x08 descriptor
#   * the central directory lists exactly ONE entry at local offset 0
#   * EOCD "entries on this disk" = 0 while "total entries" = 1  (the quirk that
#     makes Eitaa see nothing to filter)
#
# Like the rest of this module it is isolated and defensive: ANY failure returns
# the apk unchanged, and a non-apk send is byte-for-byte unaffected.

#: Environment flag for zip-wrap. DEFAULT ON (unset == on) because a raw apk
#: does not arrive at all; set MKWL_APK_ZIP=0 to go back to the raw send.
APK_ZIP_ENV = "MKWL_APK_ZIP"

#: Optional overrides for the names the recipient sees.
APK_INNER_NAME_ENV = "MKWL_APK_INNER_NAME"   # the .apk they tap
APK_OUTER_NAME_ENV = "MKWL_APK_OUTER_NAME"   # the .zip shown in chat

_FALSE = {"0", "false", "no", "off"}


def zip_enabled() -> bool:
    """True by default; False only when MKWL_APK_ZIP is explicitly a false
    value. Never raises."""
    try:
        return str(os.environ.get(APK_ZIP_ENV, "")).strip().lower() not in _FALSE
    except Exception:  # noqa: BLE001
        return True


def set_zip_env(value: bool) -> None:
    """Reflect the persisted zip-wrap setting into the live environment. Never
    raises."""
    try:
        os.environ[APK_ZIP_ENV] = "1" if value else "0"
    except Exception:  # noqa: BLE001
        pass


def build_eitaa_apk_zip(apk: bytes, inner_name: str) -> bytes:
    """Hand-craft the malformed-but-Android-valid zip around ``apk``. Standard
    zip libraries cannot produce the central-directory / EOCD quirks, so the
    bytes are laid out explicitly. Pure stdlib (struct + zlib)."""
    import struct as _s
    import zlib as _z

    name = inner_name.encode("utf-8")
    co = _z.compressobj(6, _z.DEFLATED, -15)          # raw deflate, no zlib hdr
    comp = co.compress(apk) + co.flush()
    crc = _z.crc32(apk) & 0xFFFFFFFF
    csize, usize = len(comp), len(apk)
    mtime, mdate, flags, method = 0x5AE9, 0x5D48, 0x0008, 8

    out = bytearray()
    # LOCAL FILE HEADER (offset 0): crc & compressed_size ZEROED (streamed)
    out += b"PK\x03\x04"
    out += _s.pack("<H", 20) + _s.pack("<H", flags) + _s.pack("<H", method)
    out += _s.pack("<H", mtime) + _s.pack("<H", mdate)
    out += _s.pack("<I", 0) + _s.pack("<I", 0) + _s.pack("<I", usize)
    out += _s.pack("<H", len(name)) + _s.pack("<H", 0) + name
    # FILE DATA (deflate)
    out += comp
    # DATA DESCRIPTOR (with signature): the true crc + sizes
    out += b"PK\x07\x08" + _s.pack("<I", crc) + _s.pack("<I", csize) + _s.pack("<I", usize)
    # CENTRAL DIRECTORY: exactly ONE entry, local offset 0
    cd_off = len(out)
    out += b"PK\x01\x02"
    out += _s.pack("<H", 20) + _s.pack("<H", 20) + _s.pack("<H", flags) + _s.pack("<H", method)
    out += _s.pack("<H", mtime) + _s.pack("<H", mdate)
    out += _s.pack("<I", crc) + _s.pack("<I", csize) + _s.pack("<I", usize)
    out += _s.pack("<H", len(name)) + _s.pack("<H", 0) + _s.pack("<H", 0)
    out += _s.pack("<H", 0) + _s.pack("<H", 0) + _s.pack("<I", 0) + _s.pack("<I", 0)
    out += name
    cd_size = len(out) - cd_off
    # EOCD: entries-on-this-disk = 0, total = 1  (the quirk)
    out += b"PK\x05\x06"
    out += _s.pack("<H", 0) + _s.pack("<H", 0) + _s.pack("<H", 0) + _s.pack("<H", 1)
    out += _s.pack("<I", cd_size) + _s.pack("<I", cd_off) + _s.pack("<H", 0)
    return bytes(out)


def _inner_name(file_name: str) -> str:
    """The recipient-visible .apk name. Keeps the original apk's name so it is
    recognisable; override with MKWL_APK_INNER_NAME. Never raises."""
    try:
        ov = str(os.environ.get(APK_INNER_NAME_ENV, "")).strip()
        if ov:
            return ov if ov.lower().endswith(".apk") else ov + ".apk"
        base = str(file_name)
        return base if base.lower().endswith(".apk") else base + ".apk"
    except Exception:  # noqa: BLE001
        return "برنامه.apk"


def _outer_name(file_name: str) -> str:
    """The .zip name shown in chat. Defaults to the apk's stem + .zip; override
    with MKWL_APK_OUTER_NAME. Never raises."""
    try:
        ov = str(os.environ.get(APK_OUTER_NAME_ENV, "")).strip()
        if ov:
            return ov if ov.lower().endswith(".zip") else ov + ".zip"
        base = str(file_name)
        stem = base[:-4] if base.lower().endswith(".apk") else base
        return (stem or "file") + ".zip"
    except Exception:  # noqa: BLE001
        return "file.zip"


def wrap_apk_for_send(data: bytes, file_name: str, base_mime: str):
    """Transform an apk into the Eitaa-bypass zip for upload.

    Returns a ``(bytes, file_name, mime)`` triple. When zip-wrap is ON and
    ``file_name`` is an apk, that is ``(wrapped_zip, outer_zip_name,
    'application/zip')``; the wrap is round-trip-checked first, so if it ever
    fails to reproduce the exact apk the original triple is returned instead. In
    every other case (mode off, not an apk, or ANY error) the original triple is
    returned unchanged. Never raises."""
    try:
        if is_apk(file_name) and zip_enabled():
            inner = _inner_name(file_name)
            outer = _outer_name(file_name)
            wrapped = build_eitaa_apk_zip(data, inner)
            # the recipient MUST be able to recover the exact apk; verify before
            # we commit to sending the wrapped form
            import io as _io
            import zipfile as _zip
            names = _zip.ZipFile(_io.BytesIO(wrapped)).namelist()
            if names and _zip.ZipFile(_io.BytesIO(wrapped)).read(names[0]) == data:
                return wrapped, outer, "application/zip"
    except Exception:  # noqa: BLE001 - a wrap failure must never break a send
        pass
    return data, file_name, base_mime
