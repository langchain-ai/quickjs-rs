"""Context. See spec/implementation.md §7."""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import JSError, MarshalError, QuickJSError
from quickjs_rs.globals import Globals
from quickjs_rs.runtime import Runtime


class Context:
    """A JS execution context — globals, eval, host functions.

    One runtime can own many contexts; each has its own global object
    but shares the runtime's heap and interrupt handler.
    """

    def __init__(self, runtime: Runtime, *, timeout: float = 5.0) -> None:
        if runtime._closed:
            raise QuickJSError("runtime is closed")
        self._runtime = runtime
        try:
            self._engine_ctx = _engine.QjsContext(runtime._engine_rt)
        except _engine.QuickJSError as e:
            raise QuickJSError(str(e)) from e
        self._timeout = timeout
        self._closed = False
        # The globals proxy holds a QjsHandle to globalThis. Built
        # lazily so a context created solely for eval() with no
        # globals access doesn't pay for the handle allocation.
        self._globals: Globals | None = None

    @property
    def globals(self) -> Globals:
        """Dict-like proxy for `globalThis`. See Globals for semantics."""
        if self._closed:
            raise QuickJSError("context is closed")
        if self._globals is None:
            self._globals = Globals(self, self._engine_ctx.global_object())
        return self._globals

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
        # Dispose the globals handle before closing the engine ctx —
        # otherwise the still-alive Persistent<Value> inside it would
        # outlive its runtime and trip QuickJS's gc_obj_list assertion
        # at JS_FreeRuntime time.
        if self._globals is not None:
            self._globals._handle.dispose()
            self._globals = None
        self._engine_ctx.close()
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
        """Evaluate JS code and return the result as a Python value.

        §7.3: the wall-clock timeout is measured from the start of
        each call. The runtime's interrupt handler reads the deadline
        written here and aborts execution when it elapses.
        """
        if self._closed:
            raise QuickJSError("context is closed")

        deadline = time.monotonic() + self._timeout
        self._runtime._deadline = deadline
        try:
            return self._engine_ctx.eval(
                code, module=module, strict=strict, filename=filename
            )
        except _engine.JSError as e:
            # Rust-side JSError is a create_exception! class; translate
            # to the pure-Python quickjs_rs.errors.JSError that users
            # actually import and catch. Step 6 will route subclass
            # selection (MemoryLimitError/TimeoutError/InterruptError)
            # here too.
            name, message, stack = e.args
            raise JSError(name, message, stack) from None
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None
        finally:
            self._runtime._deadline = None
