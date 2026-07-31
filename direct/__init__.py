"""Isolated headless Direct Client for Eitaa (MTProto over HTTPS, no browser).

This package is DELIBERATELY self-contained: it does not import from bot/,
eitaa/, or capture/, and the working browser-driver product does not import
from here. If anything in this experiment breaks, deleting the `direct/`
directory restores the project to exactly its prior state.

Status: foundational layer under construction (TL codec, MTProto 2.0 crypto,
session model). Network transport + live handshake are validated against the
user's authorized account in a later step.
"""

__all__ = ["aes", "crypto", "tl", "session", "errors", "mtproto", "apk_mode"]
