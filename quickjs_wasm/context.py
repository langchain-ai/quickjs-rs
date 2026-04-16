"""Context. See spec/implementation.md §7.2."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, overload

from quickjs_wasm import _msgpack
from quickjs_wasm._msgpack import Undefined
from quickjs_wasm.errors import JSError, MarshalError, QuickJSError
from quickjs_wasm.globals import Globals

if TYPE_CHECKING:
    from quickjs_wasm.handle import Handle
    from quickjs_wasm.runtime import Runtime


class Context:
    def __init__(self, runtime: Runtime, *, timeout: float = 5.0) -> None:
        self._runtime = runtime
        self._bridge = runtime._bridge
        ctx_id = self._bridge.context_new(runtime._rt_id)
        if ctx_id == 0:
            raise QuickJSError("failed to create QuickJS context")
        self._ctx_id = ctx_id
        self._timeout = timeout
        self._closed = False
        self.preserve_undefined = False
        self._globals = Globals(self._bridge, self._ctx_id)

    def __enter__(self) -> Context:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._bridge.context_free(self._ctx_id)
        self._runtime._unregister_context(self)
        self._closed = True

    def eval(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> Any:
        if self._closed:
            raise QuickJSError("context is closed")
        del filename  # filename passthrough lands when we wire §6.2's options.
        flags = 0
        if module:
            flags |= 0x1
        if strict:
            flags |= 0x4
        status, slot = self._bridge.eval(self._ctx_id, code, flags)
        if status < 0:
            raise QuickJSError(f"shim error from qjs_eval: status={status}")
        if status == 1:
            # Exception path lands with the next assertion batch. For now,
            # surface a minimal JSError so callers aren't left silent.
            self._bridge.slot_drop(self._ctx_id, slot)
            raise JSError("Error", "uncaught JS exception (stack not yet wired)")
        try:
            mp_status, payload = self._bridge.to_msgpack(self._ctx_id, slot)
            if mp_status < 0:
                raise MarshalError(
                    "value type is not yet supported by qjs_to_msgpack; "
                    "additional branches land in subsequent commits"
                )
            value = _msgpack.decode(payload)
            if isinstance(value, Undefined) and not self.preserve_undefined:
                return None
            return value
        finally:
            self._bridge.slot_drop(self._ctx_id, slot)

    def eval_handle(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> Handle:
        raise NotImplementedError("eval_handle lands with handle support (§7.2).")

    @property
    def globals(self) -> Globals:
        if self._closed:
            raise QuickJSError("context is closed")
        return self._globals

    @overload
    def function(self, fn: Callable[..., Any]) -> Callable[..., Any]: ...
    @overload
    def function(
        self, *, name: str
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...
    def function(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
    ) -> Any:
        raise NotImplementedError("function lands with host-function support (§7.2).")

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        raise NotImplementedError("register lands with host-function support (§7.2).")

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._timeout = value
