"""TL (Type Language) primitive (de)serialization for the direct client.

Only the wire primitives live here -- ints, longs, big ints, the TL "bytes"
string encoding, vectors, doubles and bools. The full layer-135 schema (method
and constructor codecs) is generated/added on top of these once the transport
+ handshake are verified live. Everything here is pure and unit-testable.
"""

from __future__ import annotations

import struct
from io import BytesIO
from typing import Callable

# Well-known TL constructor ids used by primitives.
_VECTOR_ID = 0x1CB5C415
_BOOL_TRUE = 0x997275B5
_BOOL_FALSE = 0xBC799737


class Reader:
    """Sequential reader over a TL byte buffer (little-endian)."""

    def __init__(self, data: bytes) -> None:
        self._buf = BytesIO(data)
        self._len = len(data)

    def read(self, n: int) -> bytes:
        b = self._buf.read(n)
        if len(b) != n:
            raise EOFError(f"needed {n} bytes, got {len(b)}")
        return b

    def tell(self) -> int:
        return self._buf.tell()

    def remaining(self) -> int:
        return self._len - self._buf.tell()

    def int(self, signed: bool = True) -> int:
        return int.from_bytes(self.read(4), "little", signed=signed)

    def long(self, signed: bool = True) -> int:
        return int.from_bytes(self.read(8), "little", signed=signed)

    def int128(self) -> bytes:
        return self.read(16)

    def int256(self) -> bytes:
        return self.read(32)

    def double(self) -> float:
        return struct.unpack("<d", self.read(8))[0]

    def bool(self) -> bool:
        v = self.int(signed=False)
        if v == _BOOL_TRUE:
            return True
        if v == _BOOL_FALSE:
            return False
        raise ValueError(f"invalid TL bool constructor 0x{v:08x}")

    def bytes(self) -> bytes:
        """Decode the TL byte-string length prefix + padding to 4 bytes."""
        first = self.read(1)[0]
        if first <= 253:
            length = first
            padding = (-(length + 1)) % 4
        else:
            length = int.from_bytes(self.read(3), "little")
            padding = (-length) % 4
        data = self.read(length)
        if padding:
            self.read(padding)
        return data

    def string(self) -> str:
        return self.bytes().decode("utf-8")

    def vector(self, item: Callable[["Reader"], object]) -> list:
        cid = self.int(signed=False)
        if cid != _VECTOR_ID:
            raise ValueError(f"expected vector 0x{_VECTOR_ID:08x}, got 0x{cid:08x}")
        count = self.int(signed=False)
        return [item(self) for _ in range(count)]


def int_bytes(value: int, signed: bool = True) -> bytes:
    return int(value).to_bytes(4, "little", signed=signed)


def long_bytes(value: int, signed: bool = True) -> bytes:
    return int(value).to_bytes(8, "little", signed=signed)


def double_bytes(value: float) -> bytes:
    return struct.pack("<d", value)


def bool_bytes(value: bool) -> bytes:
    return int_bytes(_BOOL_TRUE if value else _BOOL_FALSE, signed=False)


def bytes_bytes(data: bytes) -> bytes:
    """Encode a TL byte string: length prefix + data + padding to 4 bytes."""
    n = len(data)
    if n <= 253:
        out = bytes([n]) + data
    else:
        out = b"\xfe" + n.to_bytes(3, "little") + data
    if len(out) % 4:
        out += b"\x00" * (-len(out) % 4)
    return out


def string_bytes(value: str) -> bytes:
    return bytes_bytes(value.encode("utf-8"))


def vector_bytes(items: list, item_enc: Callable[[object], bytes]) -> bytes:
    out = int_bytes(_VECTOR_ID, signed=False) + int_bytes(len(items), signed=False)
    for it in items:
        out += item_enc(it)
    return out
