"""Handle. See spec/implementation.md §7.2, §7.3."""

from __future__ import annotations

import warnings
import weakref
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

from quickjs_wasm import _msgpack
from quickjs_wasm._msgpack import Undefined
from quickjs_wasm.errors import (
    InvalidHandleError,
    JSError,
    MarshalError,
    QuickJSError,
)

if TYPE_CHECKING:
    from quickjs_wasm._bridge import Bridge
    from quickjs_wasm.context import Context

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
        # Promises are out of scope for v0.1 (§14). Returning False is
        # correct for every value that isn't one; acceptance test doesn't
        # create promises.
        return False

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
        try:
            status, result_slot = self._bridge.call(
                self._ctx_id, self._slot, this_slot, arg_slots
            )
            return self._slot_to_handle_or_raise(status, result_slot)
        finally:
            for s in owned_slots:
                self._bridge.slot_drop(self._ctx_id, s)

    def call_method(self, name: str, *args: Handle | Any) -> Handle:
        self._check_live()
        # Resolve the method handle, then call it with self as `this`.
        method = self.get(name)
        try:
            return method.call(*args, this=self)
        finally:
            method.dispose()

    def new(self, *args: Handle | Any) -> Handle:
        raise NotImplementedError(
            "Handle.new (JS `new`) lands when qjs_new_instance is wired; "
            "§13 acceptance doesn't exercise it."
        )

    def to_python(self, *, allow_opaque: bool = False) -> Any:
        self._check_live()
        if allow_opaque:
            raise NotImplementedError(
                "to_python(allow_opaque=True) lands in the next commit."
            )
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

    def await_promise(self, *, deadline: float | None = None) -> Handle:
        raise NotImplementedError("await_promise lands in v0.3; see spec §7.2.")

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
