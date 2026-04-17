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

JS_SAFE_INT_MIN = -(2**53) + 1
JS_SAFE_INT_MAX = (2**53) - 1


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


def encode(value: Any) -> bytes:
    """Encode a Python value to MessagePack per §8's Python-side table.

    Raises TypeError (caller wraps as MarshalError) for types that aren't
    representable: sets, custom classes, datetimes, dict keys that aren't
    strings, etc. v0.1 has no ``default=`` hook (§8).
    """
    buf = bytearray()
    _encode_at(buf, value)
    return bytes(buf)


def _encode_at(buf: bytearray, value: Any) -> None:
    if value is None:
        buf.append(0xC0)
        return
    if isinstance(value, Undefined):
        buf.extend(b"\xc7\x00\x00")
        return
    if isinstance(value, bool):  # bool before int — bool is a subclass
        buf.append(0xC3 if value else 0xC2)
        return
    if isinstance(value, int):
        if JS_SAFE_INT_MIN <= value <= JS_SAFE_INT_MAX:
            # §8: safe-range ints marshal as JS number (float64 on the wire).
            _encode_float(buf, float(value))
        else:
            _encode_bigint(buf, value)
        return
    if isinstance(value, float):
        _encode_float(buf, value)
        return
    if isinstance(value, str):
        _encode_str(buf, value)
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        _encode_bin(buf, bytes(value))
        return
    if isinstance(value, (list, tuple)):
        _encode_array(buf, value)
        return
    if isinstance(value, dict):
        _encode_map(buf, value)
        return
    raise TypeError(
        f"value of type {type(value).__name__!r} is not marshalable to JS per §8"
    )


def _encode_float(buf: bytearray, value: float) -> None:
    buf.append(0xCB)
    buf.extend(struct.pack(">d", value))


def _encode_str(buf: bytearray, value: str) -> None:
    body = value.encode("utf-8")
    n = len(body)
    if n <= 31:
        buf.append(0xA0 | n)
    elif n <= 0xFF:
        buf.append(0xD9)
        buf.append(n)
    elif n <= 0xFFFF:
        buf.append(0xDA)
        buf.extend(n.to_bytes(2, "big"))
    elif n <= 0xFFFFFFFF:
        buf.append(0xDB)
        buf.extend(n.to_bytes(4, "big"))
    else:
        raise ValueError("string exceeds msgpack str32 limit")
    buf.extend(body)


def _encode_bin(buf: bytearray, body: bytes) -> None:
    n = len(body)
    if n <= 0xFF:
        buf.append(0xC4)
        buf.append(n)
    elif n <= 0xFFFF:
        buf.append(0xC5)
        buf.extend(n.to_bytes(2, "big"))
    elif n <= 0xFFFFFFFF:
        buf.append(0xC6)
        buf.extend(n.to_bytes(4, "big"))
    else:
        raise ValueError("bytes exceed msgpack bin32 limit")
    buf.extend(body)


def _encode_bigint(buf: bytearray, value: int) -> None:
    body = str(value).encode("utf-8")
    n = len(body)
    if n in (1, 2, 4, 8, 16):
        type_code = {1: 0xD4, 2: 0xD5, 4: 0xD6, 8: 0xD7, 16: 0xD8}[n]
        buf.append(type_code)
        buf.append(0x01)
    elif n <= 0xFF:
        buf.append(0xC7)
        buf.append(n)
        buf.append(0x01)
    elif n <= 0xFFFF:
        buf.append(0xC8)
        buf.extend(n.to_bytes(2, "big"))
        buf.append(0x01)
    elif n <= 0xFFFFFFFF:
        buf.append(0xC9)
        buf.extend(n.to_bytes(4, "big"))
        buf.append(0x01)
    else:
        raise ValueError("bigint decimal exceeds ext32 body limit")
    buf.extend(body)


def _encode_array(buf: bytearray, items: Any) -> None:
    n = len(items)
    if n <= 15:
        buf.append(0x90 | n)
    elif n <= 0xFFFF:
        buf.append(0xDC)
        buf.extend(n.to_bytes(2, "big"))
    elif n <= 0xFFFFFFFF:
        buf.append(0xDD)
        buf.extend(n.to_bytes(4, "big"))
    else:
        raise ValueError("array exceeds msgpack array32 limit")
    for item in items:
        _encode_at(buf, item)


def _encode_map(buf: bytearray, items: dict[Any, Any]) -> None:
    n = len(items)
    if n <= 15:
        buf.append(0x80 | n)
    elif n <= 0xFFFF:
        buf.append(0xDE)
        buf.extend(n.to_bytes(2, "big"))
    elif n <= 0xFFFFFFFF:
        buf.append(0xDF)
        buf.extend(n.to_bytes(4, "big"))
    else:
        raise ValueError("map exceeds msgpack map32 limit")
    for key, val in items.items():
        if not isinstance(key, str):
            raise TypeError(
                f"dict key must be str per §8, got {type(key).__name__!r}"
            )
        _encode_str(buf, key)
        _encode_at(buf, val)


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
