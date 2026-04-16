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


_FIXEXT_LENGTHS = {0xD4: 1, 0xD5: 2, 0xD6: 4, 0xD7: 8, 0xD8: 16}


def _decode_at(data: bytes, offset: int) -> tuple[Any, int]:
    if offset >= len(data):
        raise ValueError("unexpected end of msgpack input")
    b = data[offset]

    # positive fixint
    if b < 0x80:
        return b, offset + 1
    # fixmap
    if 0x80 <= b <= 0x8F:
        count = b & 0x0F
        return _decode_map(data, offset + 1, count)
    # fixarray
    if 0x90 <= b <= 0x9F:
        count = b & 0x0F
        return _decode_array(data, offset + 1, count)
    # fixstr
    if 0xA0 <= b <= 0xBF:
        length = b & 0x1F
        end = offset + 1 + length
        return data[offset + 1 : end].decode("utf-8"), end
    # nil
    if b == 0xC0:
        return None, offset + 1
    # false / true
    if b == 0xC2:
        return False, offset + 1
    if b == 0xC3:
        return True, offset + 1
    # bin 8 / 16 / 32
    if b == 0xC4:
        length = data[offset + 1]
        end = offset + 2 + length
        return data[offset + 2 : end], end
    if b == 0xC5:
        length = int.from_bytes(data[offset + 1 : offset + 3], "big")
        end = offset + 3 + length
        return data[offset + 3 : end], end
    if b == 0xC6:
        length = int.from_bytes(data[offset + 1 : offset + 5], "big")
        end = offset + 5 + length
        return data[offset + 5 : end], end
    # ext 8 / 16 / 32
    if b == 0xC7:
        length = data[offset + 1]
        ext_type = data[offset + 2]
        end = offset + 3 + length
        return _decode_ext(ext_type, data[offset + 3 : end]), end
    if b == 0xC8:
        length = int.from_bytes(data[offset + 1 : offset + 3], "big")
        ext_type = data[offset + 3]
        end = offset + 4 + length
        return _decode_ext(ext_type, data[offset + 4 : end]), end
    if b == 0xC9:
        length = int.from_bytes(data[offset + 1 : offset + 5], "big")
        ext_type = data[offset + 5]
        end = offset + 6 + length
        return _decode_ext(ext_type, data[offset + 6 : end]), end
    # float64
    if b == 0xCB:
        (value,) = struct.unpack_from(">d", data, offset + 1)
        return float(value), offset + 9
    # fixext 1 / 2 / 4 / 8 / 16
    if b in _FIXEXT_LENGTHS:
        length = _FIXEXT_LENGTHS[b]
        ext_type = data[offset + 1]
        end = offset + 2 + length
        return _decode_ext(ext_type, data[offset + 2 : end]), end
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
    # array 16 / 32
    if b == 0xDC:
        count = int.from_bytes(data[offset + 1 : offset + 3], "big")
        return _decode_array(data, offset + 3, count)
    if b == 0xDD:
        count = int.from_bytes(data[offset + 1 : offset + 5], "big")
        return _decode_array(data, offset + 5, count)
    # map 16 / 32
    if b == 0xDE:
        count = int.from_bytes(data[offset + 1 : offset + 3], "big")
        return _decode_map(data, offset + 3, count)
    if b == 0xDF:
        count = int.from_bytes(data[offset + 1 : offset + 5], "big")
        return _decode_map(data, offset + 5, count)

    raise NotImplementedError(
        f"msgpack decode for format 0x{b:02x} is not yet implemented"
    )


def _decode_array(data: bytes, offset: int, count: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    for _ in range(count):
        value, offset = _decode_at(data, offset)
        items.append(value)
    return items, offset


def _decode_map(data: bytes, offset: int, count: int) -> tuple[dict[str, Any], int]:
    """§8: maps have str keys; preserve insertion order (Python dicts already do)."""
    result: dict[str, Any] = {}
    for _ in range(count):
        key, offset = _decode_at(data, offset)
        if not isinstance(key, str):
            raise ValueError(f"msgpack map key must be str per §8, got {type(key)!r}")
        value, offset = _decode_at(data, offset)
        result[key] = value
    return result, offset


def _decode_ext(ext_type: int, body: bytes) -> Any:
    if ext_type == EXT_UNDEFINED:
        return UNDEFINED
    if ext_type == EXT_BIGINT:
        return int(body.decode("utf-8"))
    raise NotImplementedError(f"msgpack ext type {ext_type} is not yet implemented")
