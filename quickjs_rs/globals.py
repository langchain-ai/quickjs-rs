"""Globals proxy. See spec/implementation.md §7.2."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from quickjs_rs.errors import InvalidHandleError, MarshalError
from quickjs_rs.handle import Handle

if TYPE_CHECKING:
    from quickjs_rs.context import Context


class Globals:
    """Dict-like access to a context's global object.

    Semantics:

    - ``globals["x"]`` reads via JS property access on ``globalThis``
      and marshals the result per §6.6. Missing keys return ``None``
      (JS: ``globalThis.missing`` is ``undefined``), not ``KeyError``.
    - ``globals["x"] = value`` writes via ``globalThis["x"] = value``.
      ``value`` may be a Python value (marshaled per §6.6) or a
      :class:`Handle` on the same context.
    - ``"x" in globals`` is True iff ``globalThis["x"]`` exists *and*
      is not ``undefined``. Collapses JS's "own = undefined" vs
      "not defined" distinction to "not present".
    - ``get_handle("x")`` returns a :class:`Handle` wrapping the
      value — the escape hatch when ``__getitem__`` would raise
      ``MarshalError`` (function, symbol, etc).
    - ``del globals["x"]`` is not supported (§7.2 declares
      __getitem__/__setitem__/__contains__/get_handle only).
      Omitting __delitem__ makes ``del`` raise TypeError
      automatically — no explicit stub needed.
    """

    def __init__(self, context: Context, handle: Handle) -> None:
        self._context = context
        # Handle wrapper around the global object. Lifetime is
        # managed by Context.close(); Globals just borrows.
        self._handle = handle

    def __getitem__(self, key: str) -> Any:
        # Read via a short-lived child handle so the §6.6 marshaler
        # runs with allow_opaque=False. Missing keys come back as
        # undefined → None at the root per §6.6's depth-0 rule.
        with self._handle.get(key) as child:
            try:
                return child.to_python()
            except MarshalError:
                # Surface MarshalError for values like functions —
                # callers who want those should use get_handle().
                raise

    def __setitem__(self, key: str, value: Any) -> None:
        if isinstance(value, Handle):
            if value._context_id != self._handle._context_id:
                raise InvalidHandleError(
                    "handle belongs to a different context"
                )
        self._handle.set(key, value)

    def __contains__(self, key: str) -> bool:
        return self._handle.has(key)

    def get_handle(self, key: str) -> Handle:
        """Return a :class:`Handle` wrapping ``globalThis[key]``.

        Use this when the value is something ``__getitem__`` can't
        marshal (function, symbol, etc). Caller owns the handle and
        is responsible for disposing it.
        """
        return self._handle.get(key)
