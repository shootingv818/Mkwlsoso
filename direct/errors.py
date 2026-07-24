"""Exception types for the direct client."""

from __future__ import annotations


class DirectError(Exception):
    """Base class for all direct-client errors."""


class TransportError(DirectError):
    """Low-level transport / framing problem."""


class SecurityError(DirectError):
    """A security invariant failed (bad auth_key_id, msg_key mismatch, ...)."""


class RpcError(DirectError):
    """An rpc_error returned by the server."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class FloodWait(RpcError):
    """FLOOD_WAIT_X — must wait `seconds` before retrying."""

    def __init__(self, seconds: int) -> None:
        super().__init__(420, f"FLOOD_WAIT_{seconds}")
        self.seconds = seconds


def rpc_error_from(code: int, message: str) -> RpcError:
    """Map a raw rpc_error into a typed exception."""
    if message.startswith("FLOOD_WAIT_"):
        try:
            return FloodWait(int(message.rsplit("_", 1)[1]))
        except (ValueError, IndexError):
            pass
    return RpcError(code, message)
