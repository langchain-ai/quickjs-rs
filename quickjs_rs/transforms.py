"""Source transform controls for eval and module loading."""

from __future__ import annotations

from collections.abc import Callable
from enum import IntFlag

import quickjs_rs._transform as _transform


class SourceTransform(IntFlag):
    """Flags controlling the OXC-backed source transform pipeline."""

    NONE = 0
    SOURCE_TS = _transform.FLAG_SOURCE_TS
    SOURCE_TSX = _transform.FLAG_SOURCE_TSX
    STRIP_TYPESCRIPT = _transform.FLAG_STRIP_TYPESCRIPT
    TOP_LEVEL_CONST_TO_VAR = _transform.FLAG_TOP_LEVEL_CONST_TO_VAR


TransformFlags = SourceTransform | int
TransformFlagsProvider = TransformFlags | Callable[[str], TransformFlags]


def default_module_transform_flags(name: str) -> SourceTransform:
    """Return the default module transform flags for a canonical module name."""
    return SourceTransform(_transform.module_transform_flags(name))


def transform_source(
    name: str,
    source: str,
    *,
    flags: TransformFlags | None = None,
) -> str:
    """Transform one source string through a temporary isolated transformer.

    If ``flags`` is omitted, the default module policy is derived from
    ``name``: ``.ts``/``.mts``/``.cts`` strip as TypeScript, ``.tsx`` strips as
    TSX, and other names pass through unchanged.
    """
    return _transform.transform_module_source(
        name,
        source,
        flags=None if flags is None else int(flags),
    )


__all__ = [
    "SourceTransform",
    "TransformFlags",
    "TransformFlagsProvider",
    "default_module_transform_flags",
    "transform_source",
]
