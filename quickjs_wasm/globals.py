"""Globals proxy. See spec/implementation.md §7.2, §7.3."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from quickjs_wasm import _msgpack
from quickjs_wasm._msgpack import Undefined
from quickjs_wasm.errors import HostError, JSError, MarshalError, QuickJSError

if TYPE_CHECKING:
    from quickjs_wasm._bridge import Bridge
    from quickjs_wasm.handle import Handle


class Globals:
    """Dict-like proxy for the JS global object.

    Reads and writes go through qjs_get_prop / qjs_set_prop against the
    global object slot. Reads marshal the value out as a Python value;
    writes accept Python values or Handles.

    Each __getitem__ / __setitem__ refreshes against the live global —
    §7.3: "Reads perform get-global each time (no caching)".

    ``key in ctx.globals`` treats a global whose value is JS ``undefined``
    as absent, matching ``typeof x !== 'undefined'`` and what Python
    callers mean by "is this set." JS also distinguishes "own property
    set to undefined" from "never defined"; Python's ``in`` doesn't have
    vocabulary for that distinction, so it collapses. If you need the
    strict JS semantics, add ``has_own(key)`` backed by JS_HasProperty —
    not needed for v0.1.
    """

    def __init__(self, bridge: Bridge, ctx_id: int) -> None:
        self._bridge = bridge
        self._ctx_id = ctx_id

    def _global_slot(self) -> int:
        slot = self._bridge.get_global_object(self._ctx_id)
        if slot == 0:
            raise QuickJSError("failed to acquire global object")
        return slot

    def __getitem__(self, key: str) -> Any:
        global_slot = self._global_slot()
        try:
            status, value_slot = self._bridge.get_prop(self._ctx_id, global_slot, key)
            if status < 0:
                raise QuickJSError(f"shim error from qjs_get_prop: status={status}")
            if status == 1:
                try:
                    self._raise_from_exception_slot(value_slot)
                finally:
                    self._bridge.slot_drop(self._ctx_id, value_slot)
            try:
                mp_status, payload = self._bridge.to_msgpack(self._ctx_id, value_slot)
                if mp_status < 0:
                    raise MarshalError(
                        f"global {key!r} holds a value not yet marshalable"
                    )
                decoded = _msgpack.decode(payload)
                if isinstance(decoded, Undefined):
                    return None
                return decoded
            finally:
                self._bridge.slot_drop(self._ctx_id, value_slot)
        finally:
            self._bridge.slot_drop(self._ctx_id, global_slot)

    def __setitem__(self, key: str, value: Handle | Any) -> None:
        from quickjs_wasm.handle import Handle as _Handle  # avoid cycle

        global_slot = self._global_slot()
        try:
            if isinstance(value, _Handle):
                raise NotImplementedError(
                    "Handle-valued assignment lands with handle support (§7.2)"
                )
            try:
                payload = _msgpack.encode(value)
            except TypeError as exc:
                raise MarshalError(str(exc)) from exc
            status, val_slot = self._bridge.from_msgpack(self._ctx_id, payload)
            if status < 0:
                raise MarshalError(
                    f"shim error from qjs_from_msgpack: status={status}"
                )
            try:
                rc = self._bridge.set_prop(self._ctx_id, global_slot, key, val_slot)
                if rc < 0:
                    raise QuickJSError(f"shim error from qjs_set_prop: status={rc}")
                if rc == 1:
                    # qjs_set_prop returned "JS exception"; the exception
                    # sits on the context. Extract it via a fresh slot
                    # allocated by qjs_get_global_object path? Simpler:
                    # construct a bare JSError — no exception-slot API for
                    # set_prop yet. Spec §6.2 doesn't specify one; if one
                    # is wanted, add qjs_set_prop's out_slot in a later
                    # spec update. For v0.1 the bare error is enough.
                    raise JSError("Error", f"failed to set global {key!r}")
            finally:
                self._bridge.slot_drop(self._ctx_id, val_slot)
        finally:
            self._bridge.slot_drop(self._ctx_id, global_slot)

    def __contains__(self, key: str) -> bool:
        # Minimal semantic: a global is present if get returns anything
        # other than the "undefined" JS sentinel. That matches
        # `typeof x !== 'undefined'`. JS also distinguishes "has own property"
        # from "resolves to undefined" — we stay with the pragmatic version
        # here since §7.2 only commits to dict-like semantics.
        global_slot = self._global_slot()
        try:
            status, value_slot = self._bridge.get_prop(self._ctx_id, global_slot, key)
            if status < 0:
                raise QuickJSError(f"shim error from qjs_get_prop: status={status}")
            if status == 1:
                try:
                    self._raise_from_exception_slot(value_slot)
                finally:
                    self._bridge.slot_drop(self._ctx_id, value_slot)
            try:
                mp_status, payload = self._bridge.to_msgpack(self._ctx_id, value_slot)
                if mp_status < 0:
                    # Holds a non-marshalable value — present but opaque.
                    return True
                return not isinstance(_msgpack.decode(payload), Undefined)
            finally:
                self._bridge.slot_drop(self._ctx_id, value_slot)
        finally:
            self._bridge.slot_drop(self._ctx_id, global_slot)

    def get_handle(self, key: str) -> Handle:
        raise NotImplementedError("get_handle lands with handle support (§7.2).")

    def _raise_from_exception_slot(self, exc_slot: int) -> None:
        """Same extraction logic as Context._raise_from_exception_slot.

        Kept local rather than extracted to avoid a circular import
        between Context and Globals; lifting into a shared helper is
        the right move once there's a third caller.
        """
        status, payload = self._bridge.exception_to_msgpack(
            self._ctx_id, exc_slot
        )
        if status < 0:
            raise QuickJSError(
                f"shim error from qjs_exception_to_msgpack: status={status}"
            )
        record = _msgpack.decode(payload)
        if not isinstance(record, dict):
            raise QuickJSError(
                f"qjs_exception_to_msgpack returned {type(record).__name__}"
            )
        name = str(record.get("name") or "Error")
        message = str(record.get("message") or "")
        stack = record.get("stack")
        stack_str: str | None = stack if isinstance(stack, str) else None
        if name == "HostError":
            cause = self._bridge.take_last_host_exception()
            err = HostError(name, message, stack_str)
            if cause is not None:
                raise err from cause
            raise err
        raise JSError(name, message, stack_str)
