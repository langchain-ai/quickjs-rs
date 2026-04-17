"""Globals proxy. See spec/implementation.md §7.2."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import MarshalError

if TYPE_CHECKING:
    from quickjs_rs.context import Context


class Globals:
    """Dict-like access to a context's global object.

    Semantics:

    - ``globals["x"]`` reads via JS property access on ``globalThis``.
      Missing keys return ``None`` (not ``KeyError``) — matches the JS
      semantics where ``globalThis.missing`` evaluates to ``undefined``.
    - ``globals["x"] = value`` writes via ``globalThis["x"] = value``.
      The value is marshaled per §6.6.
    - ``"x" in globals`` is True iff ``globalThis["x"]`` exists *and*
      is not ``undefined``. JS makes a distinction between
      "own property whose value is undefined" and "no own property" —
      Python's dict-like API collapses both to "not present" so the
      surprise surface is smaller.
    - ``get_handle("x")`` returns a ``Handle`` for handle-valued reads
      (functions, opaque objects). Lands with full handle support in
      step 7; raises ``NotImplementedError`` until then.
    - ``globals["x"] = handle`` accepting a ``Handle`` lands with
      step 7 too.
    - ``del globals["x"]`` is not supported (§7.2 declares
      __getitem__/__setitem__/__contains__/get_handle, not
      __delitem__). Omitting __delitem__ makes ``del`` raise
      TypeError automatically — no explicit stub needed.
    """

    def __init__(self, context: Context, handle: _engine.QjsHandle) -> None:
        self._context = context
        self._handle = handle

    def __getitem__(self, key: str) -> Any:
        try:
            return self._handle.get_prop(key)
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None

    def __setitem__(self, key: str, value: Any) -> None:
        # Handle-valued assignment lands in step 7. Detect it up front
        # so the error is NotImplementedError (the Handle is well-formed;
        # the wiring just isn't there) rather than a surprising
        # MarshalError from py_to_js_value. Step 7 will key off the
        # public Handle class; for now the raw engine class is what's
        # available.
        if isinstance(value, _engine.QjsHandle):
            raise NotImplementedError(
                "Handle-valued global assignment lands in step 7 "
                "(§15). See spec/implementation.md §7.2."
            )
        try:
            self._handle.set_prop(key, value)
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None

    def __contains__(self, key: str) -> bool:
        return self._handle.has_prop(key)

    def get_handle(self, key: str) -> Any:
        raise NotImplementedError(
            "get_handle lands with full Handle support in step 7 (§15). "
            "See spec/implementation.md §7.2."
        )
