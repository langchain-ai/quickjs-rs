"""MessagePack marshaling. See spec/implementation.md §8.

Ext type 0 = undefined (empty body).
Ext type 1 = bigint (UTF-8 decimal string body).

Incremental: only the branches needed by the currently-greened assertions
are implemented. Everything else raises NotImplementedError so the caller
fails loudly when it hits uncharted territory.
"""

from __future__ import annotations

import struct
from typing import Any

EXT_UNDEFINED = 0
EXT_BIGINT = 1


def decode(data: bytes) -> Any:
    """Decode a single MessagePack value from ``data``.

    Currently recognizes only float64 (format 0xcb) and uint-coerced
    positive fixints; later commits extend this as assertions require.
    """
    value, offset = _decode_at(data, 0)
    if offset != len(data):
        raise ValueError(f"trailing {len(data) - offset} bytes after decoded value")
    return value


def _decode_at(data: bytes, offset: int) -> tuple[Any, int]:
    if offset >= len(data):
        raise ValueError("unexpected end of msgpack input")
    b = data[offset]
    if b == 0xCB:  # float64
        (value,) = struct.unpack_from(">d", data, offset + 1)
        return float(value), offset + 9
    if b < 0x80:  # positive fixint
        return b, offset + 1
    raise NotImplementedError(
        f"msgpack decode for format 0x{b:02x} is not yet implemented"
    )
