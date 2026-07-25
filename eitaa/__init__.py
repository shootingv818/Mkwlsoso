"""Eitaa browser-driver package.

The capture phase showed Eitaa Web sends messages over an encrypted,
MTProto-like binary HTTP transport (POST to *.eitaa.com/eitaa/), with no
WebSocket and no plain JSON. That makes direct request replay impractical, so
we drive the real web client through its UI instead (Browser-driver path).
"""
