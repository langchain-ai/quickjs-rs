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


class Undefined:
    """Sentinel for JS `undefined`. Use the module-level ``UNDEFINED`` instance.

    Distinct from None so that a round-trip through a future
    ``preserve_undefined=True`` context can keep the distinction (§8, §7.3).
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "Undefined"

    def __bool__(self) -> bool:
        return False


UNDEFINED = Undefined()

EXT_UNDEFINED = 0
EXT_BIGINT = 1


def decode(data: bytes) -> Any:
    """Decode a single MessagePack value from ``data``."""
    value, offset = _decode_at(data, 0)
    if offset != len(data):
        raise ValueError(f"trailing {len(data) - offset} bytes after decoded value")
    return value


def _decode_at(data: bytes, offset: int) -> tuple[Any, int]:
    if offset >= len(data):
        raise ValueError("unexpected end of msgpack input")
    b = data[offset]

    # positive fixint
    if b < 0x80:
        return b, offset + 1
    # fixstr
    if 0xA0 <= b <= 0xBF:
        length = b & 0x1F
        return data[offset + 1 : offset + 1 + length].decode("utf-8"), offset + 1 + length
    # nil
    if b == 0xC0:
        return None, offset + 1
    # false / true
    if b == 0xC2:
        return False, offset + 1
    if b == 0xC3:
        return True, offset + 1
    # ext 8 (covers zero-length ext used for undefined)
    if b == 0xC7:
        length = data[offset + 1]
        ext_type = data[offset + 2]
        body = data[offset + 3 : offset + 3 + length]
        return _decode_ext(ext_type, body), offset + 3 + length
    # float64
    if b == 0xCB:
        (value,) = struct.unpack_from(">d", data, offset + 1)
        return float(value), offset + 9
    # str 8 / 16 / 32
    if b == 0xD9:
        length = data[offset + 1]
        end = offset + 2 + length
        return data[offset + 2 : end].decode("utf-8"), end
    if b == 0xDA:
        length = int.from_bytes(data[offset + 1 : offset + 3], "big")
        end = offset + 3 + length
        return data[offset + 3 : end].decode("utf-8"), end
    if b == 0xDB:
        length = int.from_bytes(data[offset + 1 : offset + 5], "big")
        end = offset + 5 + length
        return data[offset + 5 : end].decode("utf-8"), end

    raise NotImplementedError(
        f"msgpack decode for format 0x{b:02x} is not yet implemented"
    )


def _decode_ext(ext_type: int, body: bytes) -> Any:
    if ext_type == EXT_UNDEFINED:
        return UNDEFINED
    raise NotImplementedError(f"msgpack ext type {ext_type} is not yet implemented")
