"""Handle — Python wrapper around _engine.QjsHandle. See §7.2."""

from __future__ import annotations

import warnings
from types import TracebackType
from typing import TYPE_CHECKING, Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import (
    InvalidHandleError,
    JSError,
    MarshalError,
    QuickJSError,
)

if TYPE_CHECKING:
    from quickjs_rs.context import Context


class Handle:
    """Opaque reference to a JS value. Returned by
    ``ctx.eval_handle(...)`` and by methods on other handles.

    Lifetime:
      - Context-manager usage (``with ctx.eval_handle(...) as h:``) is
        the recommended form; the `__exit__` calls ``dispose()``.
      - Manual ``dispose()`` is idempotent.
      - ``__del__`` emits :class:`ResourceWarning` if the handle is
        still live at garbage-collection time, following the Python
        stdlib convention for leaked resources (§7.3).

    Cross-context guard:
      - Every method validates that the handle was created by its
        owning context. Using a handle from context A in a method on
        context B raises :class:`InvalidHandleError` (§7.3).
    """

    def __init__(self, context: Context, engine_handle: _engine.QjsHandle) -> None:
        self._context = context
        self._engine_handle: _engine.QjsHandle | None = engine_handle
        # Snapshot the raw context pointer at construction — the
        # engine handle also carries this, but caching it on the
        # Python side lets us check cross-context use without touching
        # the (possibly-disposed) engine handle.
        self._context_id: int = engine_handle.context_id
        self._disposed = False

    # ---- Cross-context guard helpers --------------------------------

    def _require_live(self) -> _engine.QjsHandle:
        if self._disposed or self._engine_handle is None:
            raise InvalidHandleError("handle is disposed")
        if self._context._closed:
            raise InvalidHandleError("handle's owning context is closed")
        return self._engine_handle

    def _require_same_context(self, other: Handle) -> None:
        if other._context_id != self._context_id:
            raise InvalidHandleError(
                "handle belongs to a different context"
            )

    # ---- Context manager protocol -----------------------------------

    def __enter__(self) -> Handle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.dispose()

    def dispose(self) -> None:
        """Release the JS reference. Idempotent."""
        if self._disposed:
            return
        self._disposed = True
        h, self._engine_handle = self._engine_handle, None
        if h is None:
            return
        # If the owning context is already closed, the engine's Drop
        # path would have cleaned up; skip the explicit dispose call
        # to avoid touching a freed runtime.
        if not self._context._closed:
            h.dispose()

    def __del__(self) -> None:
        if self._disposed:
            return
        # Python convention: warn on leaked resources.
        try:
            warnings.warn(
                f"Handle not disposed before garbage collection "
                f"(type_of={self._safe_type_of()!r}); use "
                "`with ctx.eval_handle(...) as h:` or call "
                "h.dispose() explicitly",
                ResourceWarning,
                stacklevel=2,
            )
        except Exception:
            # Interpreter shutdown — warnings may already be torn
            # down. Swallow rather than risk a noisy __del__.
            pass
        # Still try to release the JS ref. The engine's Drop path
        # handles the case where the context has already been freed.
        try:
            if self._engine_handle is not None and not self._context._closed:
                self._engine_handle.dispose()
        except Exception:
            pass
        self._disposed = True
        self._engine_handle = None

    @property
    def disposed(self) -> bool:
        return self._disposed

    def _safe_type_of(self) -> str:
        try:
            if self._engine_handle is not None:
                return self._engine_handle.type_of
        except Exception:
            pass
        return "unknown"

    # ---- Introspection ----------------------------------------------

    @property
    def type_of(self) -> str:
        """Structural type tag — one of "null", "undefined", "boolean",
        "number", "bigint", "string", "symbol", "object", "array",
        "function". §7.2.
        """
        return self._require_live().type_of

    def is_promise(self) -> bool:
        return self._require_live().is_promise()

    # ---- Property access --------------------------------------------

    def get(self, key: str) -> Handle:
        inner = self._require_live().get(key)
        return Handle(self._context, inner)

    def get_index(self, index: int) -> Handle:
        inner = self._require_live().get_index(index)
        return Handle(self._context, inner)

    def set(self, key: str, value: Any) -> None:
        """Set a property. ``value`` may be a Python value or another
        :class:`Handle` on the same context. Cross-context handles
        raise :class:`InvalidHandleError`.
        """
        if isinstance(value, Handle):
            self._require_same_context(value)
            engine_value: Any = value._require_live()
        else:
            engine_value = value
        try:
            self._require_live().set(key, engine_value)
        except _engine.InvalidHandleError as e:
            raise InvalidHandleError(str(e)) from None
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None

    def has(self, key: str) -> bool:
        return self._require_live().has(key)

    # ---- Invocation --------------------------------------------------

    def call(self, *args: Any) -> Handle:
        """Call as a function. Args may be Python values or other
        Handles on the same context.
        """
        unwrapped = self._unwrap_args(args)
        try:
            inner = self._require_live().call(*unwrapped)
        except _engine.JSError as e:
            name, message, stack = e.args
            classified = self._context._classify_jserror(
                name, message, stack, None
            )
            raise classified from classified.__cause__
        except _engine.InvalidHandleError as e:
            raise InvalidHandleError(str(e)) from None
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None
        return Handle(self._context, inner)

    def call_method(self, name: str, *args: Any) -> Handle:
        unwrapped = self._unwrap_args(args)
        try:
            inner = self._require_live().call_method(name, *unwrapped)
        except _engine.JSError as e:
            ename, message, stack = e.args
            classified = self._context._classify_jserror(
                ename, message, stack, None
            )
            raise classified from classified.__cause__
        except _engine.InvalidHandleError as e:
            raise InvalidHandleError(str(e)) from None
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None
        return Handle(self._context, inner)

    def new(self, *args: Any) -> Handle:
        """Invoke as a JS constructor (`new fn(...)`). §7.2."""
        unwrapped = self._unwrap_args(args)
        try:
            inner = self._require_live().new_instance(*unwrapped)
        except _engine.JSError as e:
            name, message, stack = e.args
            classified = self._context._classify_jserror(
                name, message, stack, None
            )
            raise classified from classified.__cause__
        except _engine.InvalidHandleError as e:
            raise InvalidHandleError(str(e)) from None
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None
        return Handle(self._context, inner)

    def _unwrap_args(self, args: tuple[Any, ...]) -> list[Any]:
        """Swap any Handle args for their engine counterpart, first
        enforcing the cross-context invariant. Engine-side doubles the
        check; we do it here too so the error message can mention the
        Python Handle class name.
        """
        out: list[Any] = []
        for a in args:
            if isinstance(a, Handle):
                self._require_same_context(a)
                out.append(a._require_live())
            else:
                out.append(a)
        return out

    # ---- Marshaling --------------------------------------------------

    def to_python(self, *, allow_opaque: bool = False) -> Any:
        """Marshal the JS value to a Python value per §6.6.

        ``allow_opaque=True`` substitutes fresh :class:`Handle` objects
        at positions that would otherwise raise :class:`MarshalError`
        (functions, symbols, promises, proxies). The caller is
        responsible for disposing those handles.

        Cycles raise ``MarshalError`` even under ``allow_opaque`` —
        detection is indirect via the depth cap (128).
        """
        live = self._require_live()
        try:
            raw = live.to_python(allow_opaque=allow_opaque)
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None
        if allow_opaque:
            return _wrap_opaque(self._context, raw)
        return raw

    def dup(self) -> Handle:
        """Create a second handle to the same JS value. §7.2."""
        inner = self._require_live().dup()
        return Handle(self._context, inner)


def _wrap_opaque(context: Context, value: Any) -> Any:
    """Walk the Python structure returned by
    ``_engine.QjsHandle.to_python(allow_opaque=True)`` and wrap any
    embedded ``_engine.QjsHandle`` in the Python ``Handle`` class.

    The Rust side uses its native ``QjsHandle`` class as the opaque
    substitute; Python users expect the public ``Handle`` class so
    equality / type_of / context-manager behavior works.
    """
    if isinstance(value, _engine.QjsHandle):
        return Handle(context, value)
    if isinstance(value, dict):
        return {k: _wrap_opaque(context, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_wrap_opaque(context, v) for v in value]
    return value


__all__ = ["Handle"]


# Re-exported error imports kept from the original surface:
_ = (JSError, QuickJSError)
