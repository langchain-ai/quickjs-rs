"""Handle — Python wrapper around _engine.QjsHandle."""

from __future__ import annotations

import warnings
from types import TracebackType
from typing import TYPE_CHECKING, Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import (
    ConcurrentEvalError,
    InvalidHandleError,
    MarshalError,
    sync_eval_async_call_error,
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
        stdlib convention for leaked resources.

    Cross-context guard:
      - Every method validates that the handle was created by its
      owning context. Using a handle from context A in a method on
        context B raises :class:`InvalidHandleError`.
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

    def _require_live(self) -> _engine.QjsHandle:
        if self._disposed or self._engine_handle is None:
            raise InvalidHandleError("handle is disposed")
        if self._context._closed:
            raise InvalidHandleError("handle's owning context is closed")
        return self._engine_handle

    def _require_same_context(self, other: Handle) -> None:
        if other._context_id != self._context_id:
            raise InvalidHandleError("handle belongs to a different context")

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
        """Release the JS reference"""
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

    @property
    def type_of(self) -> str:
        """Structural type tag — one of "null", "undefined", "boolean",
        "number", "bigint", "string", "symbol", "object", "array",
        "function". .
        """
        return self._require_live().type_of

    def is_promise(self) -> bool:
        return self._require_live().is_promise()

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

    def call(self, *args: Any) -> Handle:
        """Call as a function. Args may be Python values or other
        Handles on the same context.
        """
        return self._invoke_sync("call", args)

    def call_method(self, name: str, *args: Any) -> Handle:
        return self._invoke_sync("call_method", args, method_name=name)

    def new(self, *args: Any) -> Handle:
        """Invoke as a JS constructor (`new fn(...)`)"""
        return self._invoke_sync("new_instance", args)

    def _invoke_sync(
        self,
        kind: str,
        args: tuple[Any, ...],
        *,
        method_name: str | None = None,
    ) -> Handle:
        """Shared body for call / call_method / new.

        sync-eval-hit-async-call detection extends to the
        Handle surface — invoking a registered async host function
        via Handle.call must raise ConcurrentEvalError just like
        sync ctx.eval does. Sets in_sync_eval before the engine
        call and consumes the sync-hit flag on return.
        """
        unwrapped = self._unwrap_args(args)
        live = self._require_live()
        engine_ctx = self._context._engine_ctx
        engine_ctx.take_sync_eval_hit_async_call()
        engine_ctx.set_in_sync_eval(True)
        inner: _engine.QjsHandle
        try:
            if kind == "call":
                inner = live.call(*unwrapped)
            elif kind == "call_method":
                assert method_name is not None
                inner = live.call_method(method_name, *unwrapped)
            else:
                inner = live.new_instance(*unwrapped)
        except _engine.JSError as e:
            if engine_ctx.take_sync_eval_hit_async_call():
                raise sync_eval_async_call_error() from None
            ename, message, stack = e.args
            classified = self._context._classify_jserror(ename, message, stack, None)
            raise classified from classified.__cause__
        except _engine.InvalidHandleError as e:
            raise InvalidHandleError(str(e)) from None
        except _engine.MarshalError as e:
            if engine_ctx.take_sync_eval_hit_async_call():
                raise sync_eval_async_call_error() from None
            raise MarshalError(str(e)) from None
        finally:
            engine_ctx.set_in_sync_eval(False)
        if engine_ctx.take_sync_eval_hit_async_call():
            raise sync_eval_async_call_error()
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

    def to_python(self, *, allow_opaque: bool = False) -> Any:
        """Marshal the JS value to a Python value per .

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
        """Create a second handle to the same JS value. ."""
        inner = self._require_live().dup()
        return Handle(self._context, inner)

    async def await_promise(self, *, timeout: float | None = None) -> Handle:
        """Drive pending jobs until this Promise settles; return a
        new Handle to the resolved value, or raise on rejection.

        If this Handle isn't a Promise, returns ``self`` unchanged —
        idiomatic for chained handle ops where the caller may or
        may not know whether they're holding a Promise.

        Respects the enclosing cancel scope (cancellation in the
        driving task cascades here). Uses the owning Context's
        per-call timeout by default (same semantics as
        ``Context.eval_async``); ``timeout=`` overrides it for this
        call only.

        Honours the concurrent-eval rule: if an ``eval_async``
        or another ``await_promise`` is in flight on the same
        Context, raises ``ConcurrentEvalError``.
        """
        import time as _time

        live = self._require_live()
        ctx = self._context
        if ctx._closed:
            raise InvalidHandleError("owning context has been closed")

        # fast path: not a Promise → return self.
        if not live.is_promise():
            return self

        if ctx._eval_async_in_flight:
            raise ConcurrentEvalError(
                "another eval_async or await_promise is already in "
                "flight on this context; use a separate context for "
                "concurrent JS workloads"
            )

        if timeout is not None:
            deadline = _time.monotonic() + timeout
        else:
            deadline = _time.monotonic() + ctx._timeout

        ctx._eval_async_in_flight = True
        ctx._runtime._deadline = deadline
        try:
            # Dup into a child Handle so the driving loop's dispose
            # at the end doesn't invalidate self — await_promise
            # shouldn't consume self.
            def dup_fn() -> Handle:
                return self.dup()

            settled = await ctx._run_inside_task_group(dup_fn, deadline)
        finally:
            ctx._runtime._deadline = None
            ctx._eval_async_in_flight = False
        return settled


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
