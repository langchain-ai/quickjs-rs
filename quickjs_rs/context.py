"""Context. See spec/implementation.md §7."""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import JSError, MarshalError, QuickJSError
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
