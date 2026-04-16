"""Context. See spec/implementation.md §7.2."""

from __future__ import annotations

import time
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, overload

import wasmtime

from quickjs_wasm import _msgpack
from quickjs_wasm._msgpack import Undefined
from quickjs_wasm.errors import (
    HostError,
    JSError,
    MarshalError,
    MemoryLimitError,
    QuickJSError,
    TimeoutError,
)
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
        # Clear the host-exception side-channel so a synthetic JS
        # "HostError" from this eval can't inherit a stale __cause__
        # from an earlier eval's host-fn raise that was caught by JS.
        # Any host raise *within* this eval overwrites the channel
        # through the host_call trampoline, so nested behavior is
        # unchanged.
        self._bridge.take_last_host_exception()

        # §7.3: timeout is measured from the start of each eval /
        # eval_handle / Handle.call call. host_interrupt checks the
        # deadline; wasmtime's epoch deadline is a backup for C-level
        # loops inside QuickJS (§9).
        deadline = time.monotonic() + self._timeout
        self._bridge.set_deadline(deadline)
        try:
            status, slot = self._bridge.eval(self._ctx_id, code, flags)
        except wasmtime.Trap as trap:
            # Two legitimate traps land here: (a) the epoch-deadline
            # backup path §9 mandates — fires only when QuickJS's own
            # interrupt hook didn't notice a deadline had passed; and
            # (b) wasm-level stack exhaustion, when a JS recursion
            # frame expanded the C stack past the configured wasm
            # data-stack limit before JS_CHECK_STACK_OVERFLOW could
            # catch it. Distinguish by checking whether the wall-clock
            # deadline actually elapsed.
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"JS evaluation exceeded {self._timeout}s "
                    f"(epoch trap: {trap})"
                ) from None
            raise JSError(
                "InternalError",
                f"wasm trap during JS evaluation: {trap}",
                None,
            ) from None
        finally:
            self._bridge.set_deadline(None)
        if status < 0:
            raise QuickJSError(f"shim error from qjs_eval: status={status}")
        if status == 1:
            try:
                self._raise_from_exception_slot(slot)
            finally:
                self._bridge.slot_drop(self._ctx_id, slot)
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

    def _raise_from_exception_slot(self, exc_slot: int) -> None:
        """Extract {name, message, stack} off a JS exception and raise.

        Routes:
        - InternalError containing "out of memory" → MemoryLimitError (§10.1)
        - InternalError containing "interrupted" → TimeoutError (§10.1)
        - name == "HostError" → HostError with __cause__ threaded (§10.2)
        - everything else → JSError with name/message/stack preserved
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
                f"qjs_exception_to_msgpack returned {type(record).__name__}, "
                "expected dict"
            )
        name = record.get("name") or "Error"
        message = record.get("message") or ""
        stack = record.get("stack")
        if not isinstance(name, str):
            name = str(name)
        if not isinstance(message, str):
            message = str(message)
        stack_str: str | None = stack if isinstance(stack, str) else None

        # §10.1 InternalError routing — quickjs-ng emits fixed strings for
        # both OOM and interrupt, confirmed against the pinned submodule:
        # quickjs.c:8082 ("out of memory") and quickjs.c:8167 ("interrupted").
        if name == "InternalError":
            if "out of memory" in message:
                raise MemoryLimitError(message)
            if "interrupted" in message:
                raise TimeoutError(message)

        if name == "HostError":
            cause = self._bridge.take_last_host_exception()
            err = HostError(name, message, stack_str)
            if cause is not None:
                raise err from cause
            raise err
        raise JSError(name, message, stack_str)

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
        """Register a Python callable as a JS global function. See §7.3."""
        if fn is None:
            # Called as @ctx.function(name="..."); return the decorator.
            if name is None:
                raise TypeError(
                    "ctx.function requires either a callable or a name= kwarg"
                )
            fn_name = name

            def decorator(inner: Callable[..., Any]) -> Callable[..., Any]:
                self.register(fn_name, inner)
                return inner

            return decorator
        # Called as @ctx.function (fn is the callable, no kwargs).
        self.register(fn.__name__, fn)
        return fn

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        if self._closed:
            raise QuickJSError("context is closed")
        self._bridge.register_host_function(self._ctx_id, name, fn)

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._timeout = value
