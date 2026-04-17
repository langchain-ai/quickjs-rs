"""Context. See spec/implementation.md §7."""

from __future__ import annotations

import inspect
import time
import traceback
from collections.abc import Callable
from types import TracebackType
from typing import Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import (
    HostError,
    InterruptError,
    JSError,
    MarshalError,
    MemoryLimitError,
    QuickJSError,
    TimeoutError,
)
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

        # §6.5 host function registry. fn_id -> Python callable.
        # Monotonically-increasing ints, never reused (cheap and
        # matches v0.2). The dispatcher looks up fn_id here.
        self._host_registry: dict[int, Callable[..., Any]] = {}
        self._next_fn_id: int = 1
        # §10.2 host-exception side channel: when a host fn raises,
        # the dispatcher stashes the Python exception here so eval's
        # HostError catch can thread it via __cause__.
        self._last_host_exception: BaseException | None = None

        # Wire the Rust-side dispatcher to our Python method.
        self._engine_ctx.set_host_call_dispatcher(self._dispatch_host_call)

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

        # §10.2: clear the side channel at each sync-eval entry so a
        # swallowed raise from an earlier eval can't attach itself as
        # __cause__ on an unrelated later HostError. Preserves the
        # v0.2 tripwire test_swallowed_host_raise_does_not_leak_cause.
        self._last_host_exception = None

        deadline = time.monotonic() + self._timeout
        self._runtime._deadline = deadline
        try:
            return self._engine_ctx.eval(
                code, module=module, strict=strict, filename=filename
            )
        except _engine.JSError as e:
            name, message, stack = e.args
            classified = self._classify_jserror(name, message, stack, deadline)
            # Preserve HostError.__cause__ that _classify_jserror
            # just threaded from the side channel. `raise ... from
            # None` would clobber it; `raise ... from e` would make
            # the _engine.JSError the cause. Instead, re-raise with
            # an explicit `from classified.__cause__` — that's None
            # for non-host errors (no cause attached) and the original
            # Python exception for HostError.
            raise classified from classified.__cause__
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None
        finally:
            self._runtime._deadline = None

    def _classify_jserror(
        self,
        name: str,
        message: str,
        stack: str | None,
        deadline: float | None,
    ) -> QuickJSError:
        """§10.4: promote a raw (name, message, stack) triple into the
        right public exception class.

        - name == "HostError": threaded with __cause__ from
          self._last_host_exception so the original Python traceback
          shows up for the user.
        - name == "InternalError" with message "interrupted":
          TimeoutError. The runtime's only source of interrupts is
          the wall-clock deadline we install from eval entry; any
          "interrupted" signal therefore means the deadline elapsed.
          A defensive guard against misclassification: if somehow the
          deadline hasn't actually passed, surface as the more
          general InterruptError instead of lying about a timeout.
        - name == "InternalError" with message "out of memory":
          MemoryLimitError. QuickJS raises this from
          JS_ThrowOutOfMemory when an allocation fails past the
          memory limit set by JS_SetMemoryLimit (§9).
        - Everything else, including stack overflow (rquickjs-0.11
          surfaces it as RangeError "Maximum call stack size
          exceeded"), falls through to plain JSError. The
          test_stack_overflow_is_jserror_not_memory tripwire (§11.1)
          asserts this routing.
        """
        if name == "HostError":
            cause = self._last_host_exception
            host_err = HostError(name, message, stack)
            if cause is not None:
                host_err.__cause__ = cause
            return host_err
        if name == "InternalError":
            if "interrupted" in message:
                # Our only interrupt source is the deadline. Prefer
                # TimeoutError if it actually elapsed; fall back to
                # InterruptError otherwise so misclassification is
                # loud rather than a false timeout claim.
                if deadline is not None and time.monotonic() >= deadline:
                    return TimeoutError(message)
                return InterruptError(message)
            if "out of memory" in message:
                return MemoryLimitError(message)
        return JSError(name, message, stack)

    # ---- Host function registration ---------------------------------

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        is_async: bool | None = None,
    ) -> Callable[..., Any]:
        """Register a Python callable as a JS global function under
        ``name``. Async/sync is auto-detected via
        ``inspect.iscoroutinefunction`` unless ``is_async`` is passed.

        Returns ``fn`` unchanged so ``@ctx.function``-style use
        preserves callable identity.
        """
        if self._closed:
            raise QuickJSError("context is closed")

        if is_async is None:
            is_async = inspect.iscoroutinefunction(fn)

        fn_id = self._next_fn_id
        self._next_fn_id += 1
        self._host_registry[fn_id] = fn
        self._engine_ctx.register_host_function(name, fn_id, is_async)
        return fn

    def function(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        is_async: bool | None = None,
    ) -> Any:
        """Decorator form — registers the decorated function as a JS
        global. Usable bare::

            @ctx.function
            def add(a, b): return a + b

        or as a factory::

            @ctx.function(name="jsName")
            def py_name(x): ...

        The JS name defaults to ``fn.__name__``; override with
        ``name=``. Async/sync auto-detected via
        ``inspect.iscoroutinefunction`` unless ``is_async=`` is
        passed.
        """
        if fn is not None:
            # Bare @ctx.function (no parens).
            js_name = name if name is not None else getattr(fn, "__name__", None)
            if not js_name:
                raise QuickJSError(
                    "host function has no __name__; use @ctx.function(name=...)"
                )
            return self.register(js_name, fn, is_async=is_async)

        # Factory form.
        def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
            js_name = name if name is not None else getattr(f, "__name__", None)
            if not js_name:
                raise QuickJSError(
                    "host function has no __name__; use @ctx.function(name=...)"
                )
            return self.register(js_name, f, is_async=is_async)

        return decorator

    def _dispatch_host_call(self, fn_id: int, args: tuple[Any, ...]) -> Any:
        """§6.5 fn_id → callable lookup and call. Invoked from the
        Rust trampoline with the GIL held.

        On user-fn exception: stash the Python exception on the
        side channel, re-raise as ``_engine.JSError(("HostError",
        message, stack))`` so the Rust side throws a JS Error with
        name="HostError". Context.eval catches the
        ``_engine.JSError`` and promotes to the pure-Python
        ``HostError`` with ``__cause__`` threaded from the side
        channel.
        """
        fn = self._host_registry.get(fn_id)
        if fn is None:
            raise _engine.JSError(
                "HostError",
                f"no host function registered for fn_id={fn_id}",
                None,
            )
        try:
            return fn(*args)
        except BaseException as exc:
            self._last_host_exception = exc
            # Match v0.2: message is str(exc), not the ExcType: str(exc)
            # prefix. The Python type is preserved through __cause__ on
            # the HostError the user actually catches.
            stack = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            raise _engine.JSError("HostError", str(exc), stack) from None
