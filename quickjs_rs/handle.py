"""Handle. See spec/implementation.md §7.2, §7.3."""

from __future__ import annotations

import warnings
import weakref
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

from quickjs_rs import _msgpack
from quickjs_rs._msgpack import Undefined
from quickjs_rs.errors import (
    ConcurrentEvalError,
    InvalidHandleError,
    JSError,
    MarshalError,
    QuickJSError,
)

if TYPE_CHECKING:
    from quickjs_rs._bridge import Bridge
    from quickjs_rs.context import Context

ValueKind = Literal[
    "null",
    "undefined",
    "boolean",
    "number",
    "bigint",
    "string",
    "symbol",
    "object",
    "function",
    "array",
]

# Keep in sync with KIND_* in wasm/shim.c.
_KIND_BY_CODE: dict[int, ValueKind] = {
    0: "null",
    1: "undefined",
    2: "boolean",
    3: "number",
    4: "bigint",
    5: "string",
    6: "symbol",
    7: "object",
    8: "function",
    9: "array",
}


class Handle:
    """Opaque, lifetime-managed reference to an in-guest JS value.

    Handles are scoped to the Context that created them. Using a Handle
    from Context A in a call on Context B raises ``InvalidHandleError``
    (§7.3).

    Dispose explicitly via ``.dispose()``, ``with handle:``, or
    ``ctx.__exit__`` — a leaked handle emits ``ResourceWarning`` when
    garbage-collected (Python convention for leaked resources). If the
    owning Context has already been closed when ``__del__`` runs, the
    slot table is already gone; the warning still fires but no drop is
    attempted, so a missed ``dispose`` during Runtime teardown is safe.
    """

    def __init__(
        self,
        context: Context,
        bridge: Bridge,
        ctx_id: int,
        slot: int,
    ) -> None:
        self._bridge = bridge
        self._ctx_id = ctx_id
        self._slot = slot
        # weakref so a lingering Handle doesn't keep the Context alive
        # past its with-block. If the ref goes stale the Context has
        # been closed and slot drops would be unsafe (§7.3).
        self._context_ref: weakref.ref[Context] = weakref.ref(context)
        self._disposed = False

    # ---- Context-manager -----------------------------------------------

    def __enter__(self) -> Handle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.dispose()

    def __del__(self) -> None:
        if self._disposed:
            return
        # Emit the ResourceWarning first — even if the context is gone
        # the leak is still real, and the user should know.
        try:
            warnings.warn(
                f"Handle leaked (ctx_id={self._ctx_id}, slot={self._slot}); "
                "dispose via .dispose(), `with handle:`, or ctx.__exit__",
                ResourceWarning,
                stacklevel=2,
            )
        except Exception:
            # Warnings machinery may be shutting down; never let __del__
            # propagate.
            pass
        ctx = self._context_ref()
        if ctx is None or getattr(ctx, "_closed", True):
            # Owning context is gone — its slot table was freed when the
            # context closed. Attempting qjs_slot_drop here would either
            # no-op (best case) or corrupt state in a future context
            # that reused the context-id slot. Just mark disposed.
            self._disposed = True
            return
        try:
            self._bridge.slot_drop(self._ctx_id, self._slot)
        except Exception:
            pass
        self._disposed = True

    # ---- Public API ----------------------------------------------------

    def dispose(self) -> None:
        if self._disposed:
            return
        ctx = self._context_ref()
        if ctx is not None and not getattr(ctx, "_closed", True):
            self._bridge.slot_drop(self._ctx_id, self._slot)
        self._disposed = True

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def type_of(self) -> ValueKind:
        self._check_live()
        code = self._bridge.type_of(self._ctx_id, self._slot)
        return _KIND_BY_CODE.get(code, "undefined")

    @property
    def is_promise(self) -> bool:
        # §7.2, v0.2: backed by qjs_is_promise. The v0.1 stub always
        # returned False because promises were out of scope; v0.2's
        # await_promise needs the real answer, and callers doing
        # `if h.is_promise: await h.await_promise()` deserve a working
        # check.
        self._check_live()
        return self._bridge.is_promise(self._ctx_id, self._slot)

    def get(self, key: str | int) -> Handle:
        self._check_live()
        if isinstance(key, int):
            # Use qjs_get_prop with the stringified key; qjs_get_prop_u32
            # would be faster but isn't wired yet and isn't on the v0.1
            # acceptance path.
            key = str(key)
        status, slot = self._bridge.get_prop(self._ctx_id, self._slot, key)
        return self._slot_to_handle_or_raise(status, slot)

    def set(self, key: str, value: Handle | Any) -> None:
        self._check_live()
        val_slot, borrowed = self._coerce_to_slot(value)
        try:
            rc = self._bridge.set_prop(self._ctx_id, self._slot, key, val_slot)
            if rc < 0:
                raise QuickJSError(f"shim error from qjs_set_prop: status={rc}")
            if rc == 1:
                raise JSError("Error", f"failed to set property {key!r}")
        finally:
            if not borrowed:
                self._bridge.slot_drop(self._ctx_id, val_slot)

    def call(self, *args: Handle | Any, this: Handle | None = None) -> Handle:
        self._check_live()
        if this is not None:
            self._check_same_context(this)
        this_slot = this._slot if this is not None else 0
        arg_slots, owned_slots = self._coerce_args(args)
        # §7.4 / §10.3: Handle.call is a sync entry point into JS.
        # Same guard as Context.eval — flag async dispatches during
        # the call so ConcurrentEvalError surfaces to the caller.
        self._bridge.take_sync_eval_hit_async_call()
        self._bridge._in_sync_eval = True
        try:
            try:
                status, result_slot = self._bridge.call(
                    self._ctx_id, self._slot, this_slot, arg_slots
                )
                return self._slot_to_handle_or_raise(status, result_slot)
            finally:
                self._bridge._in_sync_eval = False
                for s in owned_slots:
                    self._bridge.slot_drop(self._ctx_id, s)
        finally:
            if self._bridge.take_sync_eval_hit_async_call():
                raise ConcurrentEvalError(
                    "sync Handle.call encountered a registered async "
                    "host function; drive the promise via await_promise "
                    "(inside an async context) instead."
                )

    def call_method(self, name: str, *args: Handle | Any) -> Handle:
        self._check_live()
        # Resolve the method handle, then call it with self as `this`.
        method = self.get(name)
        try:
            return method.call(*args, this=self)
        finally:
            method.dispose()

    def new(self, *args: Handle | Any) -> Handle:
        """Call this handle as a JS constructor — equivalent to `new self(...)`.

        See spec §7.2. Use when ``self.type_of == "function"`` and the
        function is constructor-callable (most are, except arrow
        functions which JS itself throws TypeError on when ``new``d).
        """
        self._check_live()
        arg_slots, owned_slots = self._coerce_args(args)
        # §7.4 / §10.3: same sync-eval-async-hostfn guard as
        # Handle.call. Constructors that invoke async host functions
        # (unusual but possible via a constructor body) need the
        # same entry-point-boundary surface.
        self._bridge.take_sync_eval_hit_async_call()
        self._bridge._in_sync_eval = True
        try:
            try:
                status, result_slot = self._bridge.new_instance(
                    self._ctx_id, self._slot, arg_slots
                )
                return self._slot_to_handle_or_raise(status, result_slot)
            finally:
                self._bridge._in_sync_eval = False
                for s in owned_slots:
                    self._bridge.slot_drop(self._ctx_id, s)
        finally:
            if self._bridge.take_sync_eval_hit_async_call():
                raise ConcurrentEvalError(
                    "sync Handle.new encountered a registered async "
                    "host function during construction; redesign the "
                    "constructor to avoid async host calls, or use "
                    "await_promise on a promise-returning factory "
                    "instead."
                )

    def to_python(self, *, allow_opaque: bool = False) -> Any:
        """Marshal this handle out to a Python value.

        With ``allow_opaque=False`` (default): round-trip via msgpack.
        Any child that would fail marshaling — a function, symbol, or
        a graph that exceeds the depth limit (including cycles) —
        raises :class:`MarshalError`.

        With ``allow_opaque=True``: walk the value recursively.
        Marshalable leaves are materialized; children that would
        otherwise fail get substituted with a child :class:`Handle` that
        the caller is responsible for disposing. Cycles still raise
        :class:`MarshalError` — a self-referential object with a Handle
        at the cycle point isn't meaningfully different from raising,
        and detecting cycles cheaply would require an extra shim
        export (``JS_IsSameValue``) we don't need for v0.1. The
        recursion depth cap (128, matching the encoder's
        ``MARSHAL_MAX_DEPTH``) bounds the walk so a cyclic object
        fails fast.
        """
        self._check_live()
        if allow_opaque:
            return self._to_python_opaque(depth=0)
        mp_status, payload = self._bridge.to_msgpack(self._ctx_id, self._slot)
        if mp_status < 0:
            raise MarshalError(
                "handle holds a value that is not marshalable to Python "
                "(function, symbol, circular reference, or similar). "
                "Pass allow_opaque=True to substitute child Handles."
            )
        value = _msgpack.decode(payload)
        if isinstance(value, Undefined):
            ctx = self._context_ref()
            preserve = getattr(ctx, "preserve_undefined", False) if ctx else False
            if not preserve:
                return None
        return value

    # Matches MARSHAL_MAX_DEPTH in wasm/shim.c.
    _OPAQUE_MAX_DEPTH = 128

    def _to_python_opaque(self, *, depth: int) -> Any:
        """Recursive walk for to_python(allow_opaque=True).

        Called only on a live Handle. Produces marshalable values where
        possible and substitutes child Handles where not. The caller
        (top-level ``to_python``) doesn't consume ``self`` — a caller
        that wants to dispose the root handle does so themselves.
        """
        if depth > self._OPAQUE_MAX_DEPTH:
            raise MarshalError(
                f"to_python(allow_opaque=True) exceeded depth limit "
                f"({self._OPAQUE_MAX_DEPTH}); the value likely contains "
                "a cycle"
            )
        kind = self.type_of
        if kind in ("null", "undefined", "boolean", "number", "bigint",
                    "string"):
            # Atomic — msgpack handles it; decode and return.
            mp_status, payload = self._bridge.to_msgpack(self._ctx_id, self._slot)
            if mp_status < 0:
                raise MarshalError(
                    f"unexpected msgpack failure on atomic kind {kind!r}"
                )
            value = _msgpack.decode(payload)
            if isinstance(value, Undefined):
                ctx = self._context_ref()
                preserve = getattr(ctx, "preserve_undefined", False) if ctx else False
                if not preserve:
                    return None
            return value

        if kind in ("function", "symbol"):
            # Not marshalable — return a child Handle that duplicates
            # the underlying slot so disposing `self` doesn't invalidate
            # the returned Handle.
            return self._dup_as_child_handle()

        if kind == "array":
            # Walk indices. JS arrays are dense-indexed 0..length-1; we
            # materialize each slot as its own Handle, recurse, and
            # dispose the intermediate.
            length_handle = self.get("length")
            try:
                length = length_handle.to_python()
            finally:
                length_handle.dispose()
            if not isinstance(length, (int, float)):
                raise MarshalError(
                    f"array.length marshaled as {type(length).__name__}, "
                    "expected number"
                )
            result: list[Any] = []
            for i in range(int(length)):
                child = self.get(i)
                try:
                    result.append(child._to_python_opaque(depth=depth + 1))
                finally:
                    child.dispose()
            return result

        if kind == "object":
            # Special-case Uint8Array — bytes round-trip through the
            # atomic path even though type_of returns "object".
            mp_status, payload = self._bridge.to_msgpack(
                self._ctx_id, self._slot
            )
            if mp_status == 0:
                # Fully marshalable (plain object with marshalable
                # children). Decode and return.
                return _msgpack.decode(payload)
            # Otherwise, walk own enumerable string-keyed properties.
            # Use qjs_get_prop per key; iterate via Object.keys (cheap
            # enough for v0.1, avoids adding a qjs_own_keys export).
            keys_handle = self._own_keys()
            try:
                keys = keys_handle.to_python()
            finally:
                keys_handle.dispose()
            if not isinstance(keys, list):
                raise MarshalError(
                    f"Object.keys returned {type(keys).__name__}, expected list"
                )
            result_map: dict[str, Any] = {}
            for key in keys:
                if not isinstance(key, str):
                    raise MarshalError(
                        f"Object.keys returned non-string entry: {key!r}"
                    )
                child = self.get(key)
                try:
                    result_map[key] = child._to_python_opaque(depth=depth + 1)
                finally:
                    child.dispose()
            return result_map

        # Unknown kind (shouldn't happen given the full enumeration
        # above, but defensive).
        return self._dup_as_child_handle()

    def _dup_as_child_handle(self) -> Handle:
        new_slot = self._bridge.slot_dup(self._ctx_id, self._slot)
        if new_slot == 0:
            raise MarshalError("failed to dup handle slot")
        ctx = self._context_ref()
        if ctx is None:
            raise InvalidHandleError("owning context closed")
        return Handle(ctx, self._bridge, self._ctx_id, new_slot)

    def _own_keys(self) -> Handle:
        """Return a Handle to Object.keys(self). Used by the allow_opaque
        walk to enumerate own enumerable string-keyed properties without
        adding a dedicated shim export."""
        # Resolve Object.keys via the global object, call it with self
        # as the single argument.
        ctx = self._context_ref()
        if ctx is None:
            raise InvalidHandleError("owning context closed")
        keys_fn_handle = ctx.eval_handle("Object.keys")
        try:
            return keys_fn_handle.call(self)
        finally:
            keys_fn_handle.dispose()

    async def await_promise(self, *, timeout: float | None = None) -> Handle:
        """Drive pending jobs until this Promise settles; return a new
        Handle to the resolved value, or raise on rejection.

        See §7.2, §7.4. Must be called inside an async context. If this
        Handle isn't a Promise (``is_promise`` is False), returns
        ``self`` unchanged — idiomatic for chained handle ops where
        the caller may or may not know whether they're holding a
        Promise.

        Respects the enclosing cancel scope (cancellation in the
        driving task cascades here). Uses the owning Context's
        cumulative eval_async budget unless ``timeout=`` is passed,
        in which case that value applies for this call only (same
        semantics as ``eval_async``'s ``timeout=`` kwarg).

        Concurrency: honours the §7.4 concurrent-eval rule. If an
        ``eval_async`` or another ``await_promise`` is in flight on
        the same Context, raises ``ConcurrentEvalError``.
        """
        import time as _time  # local to avoid a second module-level import

        from quickjs_rs.errors import ConcurrentEvalError

        self._check_live()
        ctx = self._context_ref()
        if ctx is None or getattr(ctx, "_closed", True):
            raise InvalidHandleError("owning context has been closed")

        # §7.4 fast path: not a Promise → return self. The caller
        # may have written code like `h.await_promise()` where `h`
        # sometimes is a plain value (the JS-side callee decided
        # whether to return a Promise at runtime). Short-circuit
        # preserves that ergonomic pattern.
        if not self._bridge.is_promise(self._ctx_id, self._slot):
            return self

        # §7.4 concurrent-eval guard — same rule as eval_async.
        if ctx._eval_async_in_flight:
            raise ConcurrentEvalError(
                "another eval_async or await_promise is already in "
                "flight on this context; use a separate context for "
                "concurrent JS workloads"
            )

        # §7.4 timeout semantics: per-call override or cumulative.
        if timeout is not None:
            deadline = _time.monotonic() + timeout
        else:
            deadline = ctx._cumulative_deadline

        ctx._eval_async_in_flight = True
        self._bridge.take_last_host_exception()
        self._bridge.set_deadline(deadline)
        try:
            # §7.4 driving-flow: open a TaskGroup, dup the slot
            # inside it (so any host tasks dispatched during promise
            # settlement are children of the group), drive.
            # Without the dup, the loop would invalidate self's slot
            # and break subsequent ops on self — await_promise
            # shouldn't consume self.
            def dup_slot() -> int:
                new_slot = self._bridge.slot_dup(self._ctx_id, self._slot)
                if new_slot == 0:
                    raise QuickJSError(
                        "failed to dup promise slot for await"
                    )
                return new_slot

            settled_slot = await ctx._run_inside_task_group(
                dup_slot, deadline
            )
        finally:
            self._bridge.set_deadline(None)
            ctx._eval_async_in_flight = False

        return Handle(ctx, self._bridge, self._ctx_id, settled_slot)

    # ---- Internals ------------------------------------------------------

    def _check_live(self) -> None:
        if self._disposed:
            raise InvalidHandleError("handle has been disposed")
        ctx = self._context_ref()
        if ctx is None or getattr(ctx, "_closed", True):
            raise InvalidHandleError("owning context has been closed")

    def _check_same_context(self, other: Handle) -> None:
        if other._ctx_id != self._ctx_id:
            raise InvalidHandleError(
                f"handle from ctx_id={other._ctx_id} used in operation on "
                f"ctx_id={self._ctx_id}"
            )
        if other._disposed:
            raise InvalidHandleError("handle has been disposed")

    def _coerce_to_slot(self, value: Handle | Any) -> tuple[int, bool]:
        """Return (slot_id, borrowed). borrowed=True means the slot belongs
        to a live Handle and must NOT be dropped by the caller; borrowed=
        False means we freshly allocated the slot via from_msgpack and
        the caller owns it."""
        if isinstance(value, Handle):
            self._check_same_context(value)
            return value._slot, True
        try:
            payload = _msgpack.encode(value)
        except TypeError as exc:
            raise MarshalError(str(exc)) from exc
        status, slot = self._bridge.from_msgpack(self._ctx_id, payload)
        if status < 0:
            raise MarshalError(
                f"shim error from qjs_from_msgpack: status={status}"
            )
        return slot, False

    def _coerce_args(
        self, args: tuple[Handle | Any, ...]
    ) -> tuple[list[int], list[int]]:
        slots: list[int] = []
        owned: list[int] = []
        try:
            for a in args:
                slot, borrowed = self._coerce_to_slot(a)
                slots.append(slot)
                if not borrowed:
                    owned.append(slot)
        except Exception:
            for s in owned:
                self._bridge.slot_drop(self._ctx_id, s)
            raise
        return slots, owned

    def _slot_to_handle_or_raise(self, status: int, slot: int) -> Handle:
        ctx = self._context_ref()
        if ctx is None or getattr(ctx, "_closed", True):
            # Context died between our call entry and now — shouldn't
            # happen with single-threaded usage, but surface cleanly.
            if slot:
                try:
                    self._bridge.slot_drop(self._ctx_id, slot)
                except Exception:
                    pass
            raise InvalidHandleError("owning context closed during call")
        if status < 0:
            if slot:
                self._bridge.slot_drop(self._ctx_id, slot)
            raise QuickJSError(f"shim error from handle operation: status={status}")
        if status == 1:
            # Bridge.raise_from_exception_slot handles §10.1 / §10.2
            # routing (HostError / TimeoutError / MemoryLimitError /
            # JSError); drop the slot in any path.
            try:
                self._bridge.raise_from_exception_slot(self._ctx_id, slot)
            finally:
                self._bridge.slot_drop(self._ctx_id, slot)
            # Defensive: raise_from_exception_slot always raises, but
            # this line prevents a silent None return from a method
            # typed `-> Handle` if the contract ever changes.
            raise QuickJSError("exception path failed to raise")
        return Handle(ctx, self._bridge, self._ctx_id, slot)
