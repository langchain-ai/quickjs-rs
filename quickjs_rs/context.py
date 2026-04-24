"""Context. See README.md section 7."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import (
    ConcurrentEvalError,
    DeadlockError,
    HostError,
    InterruptError,
    JSError,
    MarshalError,
    MemoryLimitError,
    QuickJSError,
    TimeoutError,
    sync_eval_async_call_error,
    sync_eval_handle_async_call_error,
)
from quickjs_rs.globals import Globals
from quickjs_rs.handle import Handle
from quickjs_rs.runtime import Runtime

_HOST_ERROR_SANITIZED_MESSAGE = "Host function failed"


def _detect_is_async(fn: Callable[..., Any]) -> bool:
    """section 7.4: infer async-ness of a registered host function.

    Uses ``inspect.iscoroutinefunction`` directly. If it says False
    but the ``__wrapped__`` chain reveals a coroutine function
    underneath (common when a decorator dropped the coroutine
    marker), raise ``TypeError`` rather than silently register as
    sync — the sync path fails at runtime in confusing ways. The
    error message tells the user to pass ``is_async=True``.

    C extensions, callable classes with async ``__call__``, and
    anything else iscoroutinefunction can't see fall through to
    False here; the ``is_async=True/False`` override is the
    escape hatch.
    """
    if inspect.iscoroutinefunction(fn):
        return True
    probe: Any = fn
    for _ in range(5):
        wrapped = getattr(probe, "__wrapped__", None)
        if wrapped is None:
            break
        probe = wrapped
        if inspect.iscoroutinefunction(probe):
            raise TypeError(
                f"could not auto-detect async/sync for {fn!r}: the "
                "callable is not itself a coroutine function but its "
                "__wrapped__ chain contains one. This usually means "
                "a decorator dropped the coroutine marker. Pass "
                "is_async=True explicitly to ctx.register / "
                "@ctx.function(is_async=True)."
            )
    return False


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

        # section 6.5 host function registry. fn_id -> Python callable.
        # Monotonically-increasing ints, never reused (cheap and
        # matches previous implementation). The dispatcher looks up fn_id here.
        self._host_registry: dict[int, Callable[..., Any]] = {}
        self._next_fn_id: int = 1
        # section 10.2 host-exception side channel: when a host fn raises,
        # the dispatcher stashes the Python exception here so eval's
        # HostError catch can thread it via __cause__.
        self._last_host_exception: BaseException | None = None

        # section 7.4 async machinery. _eval_async_in_flight is the
        # concurrent-eval guard — only one eval_async / await_promise
        # at a time per context. _cumulative_deadline is the rolling
        # budget shared across eval_async calls; per-call timeout=
        # overrides for one call only. _pending_tasks tracks the
        # asyncio tasks we've spawned for async host calls so the
        # driving loop knows when to wait vs raise DeadlockError.
        # _pending_completed is the wake-up event the driving loop
        # waits on between tasks. _active_task_group is set during
        # _run_inside_task_group so the async host dispatcher schedules
        # into it instead of loop.create_task.
        self._cumulative_deadline = time.monotonic() + timeout
        self._eval_async_in_flight = False
        self._pending_tasks: dict[int, asyncio.Task[Any]] = {}
        self._pending_completed: asyncio.Event | None = None
        self._active_task_group: asyncio.TaskGroup | None = None

        # Wire the Rust-side dispatchers to our Python methods.
        self._engine_ctx.set_host_call_dispatcher(self._dispatch_host_call)
        self._engine_ctx.set_async_host_dispatcher(self._dispatch_async_host_call)

    @property
    def globals(self) -> Globals:
        """Dict-like proxy for `globalThis`. See Globals for semantics."""
        if self._closed:
            raise QuickJSError("context is closed")
        if self._globals is None:
            engine_handle = self._engine_ctx.global_object()
            self._globals = Globals(self, Handle(self, engine_handle))
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

    def eval_handle(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> Handle:
        """Evaluate JS code and return the result as an opaque Handle.

        Unlike :meth:`eval`, the result is never marshaled — functions,
        symbols, promises, and other opaque values all come back as
        Handles. section 7.2.
        """
        if self._closed:
            raise QuickJSError("context is closed")

        self._last_host_exception = None
        self._engine_ctx.take_sync_eval_hit_async_call()
        self._engine_ctx.set_in_sync_eval(True)
        deadline = time.monotonic() + self._timeout
        self._runtime._deadline = deadline
        inner: _engine.QjsHandle
        try:
            inner = self._engine_ctx.eval_handle(
                code,
                module=module,
                strict=strict,
                filename=filename,
            )
        except _engine.JSError as e:
            name, message, stack = e.args
            if self._engine_ctx.take_sync_eval_hit_async_call():
                raise sync_eval_handle_async_call_error() from None
            classified = self._classify_jserror(name, message, stack, deadline)
            raise classified from classified.__cause__
        except _engine.MarshalError as e:
            if self._engine_ctx.take_sync_eval_hit_async_call():
                raise sync_eval_handle_async_call_error() from None
            raise MarshalError(str(e)) from None
        finally:
            self._engine_ctx.set_in_sync_eval(False)
            self._runtime._deadline = None
        if self._engine_ctx.take_sync_eval_hit_async_call():
            raise sync_eval_handle_async_call_error()
        return Handle(self, inner)

    def eval(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> Any:
        """Evaluate JS code and return the result as a Python value.

        section 7.3: the wall-clock timeout is measured from the start of
        each call. The runtime's interrupt handler reads the deadline
        written here and aborts execution when it elapses.
        """
        if self._closed:
            raise QuickJSError("context is closed")

        # section 10.2: clear the side channel at each sync-eval entry so a
        # swallowed raise from an earlier eval can't attach itself as
        # __cause__ on an unrelated later HostError. Preserves the
        # previous implementation tripwire test_swallowed_host_raise_does_not_leak_cause.
        self._last_host_exception = None
        # section 7.4: clear any stale sync-eval-hit-async-call flag left
        # from a prior eval that itself raised before consuming it.
        self._engine_ctx.take_sync_eval_hit_async_call()
        self._engine_ctx.set_in_sync_eval(True)

        deadline = time.monotonic() + self._timeout
        self._runtime._deadline = deadline
        result: Any
        try:
            result = self._engine_ctx.eval(
                code,
                module=module,
                strict=strict,
                filename=filename,
            )
        except _engine.JSError as e:
            name, message, stack = e.args
            # section 7.4: if an async host fn fired during this sync eval,
            # the user's real bug is "sync eval on a context with
            # async host fns". Surface that instead of whatever
            # downstream error the async-rejected promise produced.
            if self._engine_ctx.take_sync_eval_hit_async_call():
                raise sync_eval_async_call_error() from None
            classified = self._classify_jserror(name, message, stack, deadline)
            # Preserve HostError.__cause__ that _classify_jserror
            # just threaded from the side channel.
            raise classified from classified.__cause__
        except _engine.MarshalError as e:
            if self._engine_ctx.take_sync_eval_hit_async_call():
                raise sync_eval_async_call_error() from None
            raise MarshalError(str(e)) from None
        finally:
            self._engine_ctx.set_in_sync_eval(False)
            self._runtime._deadline = None

        # Normal return: still check the flag. An async host fn that
        # fired and whose rejection was caught by JS wouldn't raise,
        # but it's still a bug — the user's code ignored an async-
        # host result. section 7.4.
        if self._engine_ctx.take_sync_eval_hit_async_call():
            raise sync_eval_async_call_error()
        return result

    def _classify_jserror(
        self,
        name: str,
        message: str,
        stack: str | None,
        deadline: float | None,
    ) -> QuickJSError:
        """section 10.4: promote a raw (name, message, stack) triple into the
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
          memory limit set by JS_SetMemoryLimit (section 9).
        - Everything else, including stack overflow (rquickjs-0.11
          surfaces it as RangeError "Maximum call stack size
          exceeded"), falls through to plain JSError. The
          test_stack_overflow_is_jserror_not_memory tripwire (section 11.1)
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
            is_async = _detect_is_async(fn)

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

    # ---- Async eval --------------------------------------------------

    async def eval_async(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
        timeout: float | None = None,
    ) -> Any:
        """Evaluate code with top-level await + async host-call support.

        See section 7.4. Two modes:

        * ``module=False`` (default): script-mode eval with
          JS_EVAL_FLAG_ASYNC. Top-level ``await`` works; the return
          value is the last expression of the script (wrapped as
          ``{value, done}`` under the hood and unwrapped here).
        * ``module=True`` (previous implementation): real ES-module eval. ``import`` /
          ``export`` work. Module-scoped bindings (``let``,
          ``const``, ``var``, functions) do NOT leak to global.
          Returns ``None`` — ES modules complete with ``undefined``.
          To surface a value, set ``globalThis.result = ...`` in the
          module and read it with a sync ``ctx.eval("result")``.

        Breaking change from previous implementation: the default used to be
        ``module=True``. Under previous implementation, ``module=True`` means real ES
        modules with different scoping — silently flipping the
        behavior of existing code would be worse than requiring the
        explicit opt-in. See README.md section 7.

        Cancellation: if the enclosing asyncio task is cancelled,
        the driving loop rejects in-flight host-call Promises with a
        HostCancellationError and runs one final pending-jobs drain
        so JS catch/finally handlers execute. If JS absorbs the
        cancellation (catches and returns a value), eval_async
        returns that value without re-raising asyncio.CancelledError.
        """
        settled_handle = await self._eval_and_drive(
            code,
            module=module,
            strict=strict,
            filename=filename,
            timeout=timeout,
        )
        # Settled is a Handle; marshal to Python. Two unwrap paths:
        #
        #  * module=True: ES-module eval. The settled value is the
        #    Promise from Module::evaluate, which resolves to
        #    undefined. Returning None short-circuits that —
        #    reading `.value` off `undefined` would raise.
        #  * module=False: script-mode TLA. quickjs-ng wraps the
        #    result as {value, done} when JS_EVAL_FLAG_ASYNC is set
        #    — we unwrap `.value` below.
        try:
            if module:
                return None
            value_handle = settled_handle.get("value")
            try:
                return value_handle.to_python()
            finally:
                value_handle.dispose()
        finally:
            settled_handle.dispose()

    async def eval_handle_async(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
        timeout: float | None = None,
    ) -> Handle:
        """Same driving flow as :meth:`eval_async`, but return the
        settled value as a :class:`Handle` rather than marshaling.

        ``module=True``: returns a Handle to ``undefined`` (ES
        modules complete with undefined). The module's exports are
        not directly exposed — to access them, use bare imports
        from another module or read globals the module set.

        Breaking change from previous implementation: the default used to be
        ``module=True``. See ``eval_async`` for the rationale.
        """
        settled_handle = await self._eval_and_drive(
            code,
            module=module,
            strict=strict,
            filename=filename,
            timeout=timeout,
        )
        if module:
            # The settled promise resolves to undefined; just return
            # the handle as-is so the caller gets something uniform.
            return settled_handle
        # Script-mode TLA: unwrap the {value, done} envelope.
        try:
            return settled_handle.get("value")
        finally:
            settled_handle.dispose()

    async def _eval_and_drive(
        self,
        code: str,
        *,
        module: bool,
        strict: bool,
        filename: str,
        timeout: float | None,
    ) -> Handle:
        """Shared prologue + eval + driving loop for eval_async and
        eval_handle_async. Returns a Handle to the settled value
        (fulfilled result or {value, done} envelope for module mode).
        Raises on JS exception via the section 10.4 classifier.
        """
        if self._closed:
            raise QuickJSError("context is closed")
        # section 7.4 concurrency rule. Check BEFORE touching the engine so
        # a violation is cheap and leaves no side effects.
        if self._eval_async_in_flight:
            raise ConcurrentEvalError(
                "another eval_async is already in flight on this "
                "context; use a separate context for concurrent JS "
                "workloads"
            )
        self._last_host_exception = None
        # section 7.4 timeout semantics: per-call override or cumulative.
        if timeout is not None:
            deadline = time.monotonic() + timeout
        else:
            deadline = self._cumulative_deadline
            # Pre-check: the interrupt handler only fires during
            # bytecode execution; a near-instant eval that completes
            # before the next interrupt check would silently succeed
            # past an expired budget. Raising up front matches previous implementation
            # and the tripwire test_sync_eval_does_not_decrement_
            # cumulative_budget's companion on the async side.
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "eval_async's cumulative timeout budget has "
                    "elapsed; create a new context or pass timeout= "
                    "to this call for a fresh budget"
                )

        self._eval_async_in_flight = True
        self._runtime._deadline = deadline
        try:
            settled = await self._run_inside_task_group(
                lambda: self._eval_for_async(code, module, strict, filename, deadline),
                deadline,
            )
        finally:
            self._runtime._deadline = None
            self._eval_async_in_flight = False
        return settled

    def _eval_for_async(
        self,
        code: str,
        module: bool,
        strict: bool,
        filename: str,
        deadline: float,
    ) -> Handle:
        """Synchronous eval inside an already-open TaskGroup scope.
        Any async host calls dispatched during this eval become
        children of the group (section 7.4).

        Two eval paths:
        * ``module=True`` (previous implementation): ``eval_module_async`` uses
          ``Module::evaluate`` — imports + exports work, module
          scoping applies, result is a Promise that resolves to
          undefined.
        * ``module=False``: script-mode eval with
          JS_EVAL_FLAG_ASYNC (``promise=True``) — the previous implementation TLA
          path. Result is a Promise that resolves to the
          ``{value, done}`` envelope.
        """
        try:
            if module:
                inner = self._engine_ctx.eval_module_async(
                    code,
                    filename=filename,
                )
            else:
                inner = self._engine_ctx.eval_handle(
                    code,
                    module=False,
                    strict=strict,
                    promise=True,
                    filename=filename,
                )
        except _engine.JSError as e:
            name, message, stack = e.args
            classified = self._classify_jserror(name, message, stack, deadline)
            raise classified from classified.__cause__
        except _engine.MarshalError as e:
            raise MarshalError(str(e)) from None
        return Handle(self, inner)

    async def _run_inside_task_group(
        self,
        get_handle: Callable[[], Handle],
        deadline: float,
    ) -> Handle:
        """section 7.4 driving flow. Open a TaskGroup, obtain the initial
        Handle via ``get_handle`` (runs synchronously inside the
        group so any async host calls dispatched during initial eval
        become children of the group), then drive the resulting
        Promise.

        The TaskGroup must wrap the initial eval so host-call tasks
        scheduled *during* the initial synchronous phase become
        children of the group. Otherwise cancellation doesn't
        cascade to them and they leak — same seam as previous implementation's fix
        that caught the cancel-leak bug.

        Cancellation flow (section 7.4):

        1. Driving loop's ``event.wait()`` raises CancelledError.
        2. We reject every in-flight Promise with a
           HostCancellationError BEFORE TaskGroup teardown so the
           JS-side rejections see live resolvers.
        3. Re-raise to tear down the TaskGroup (cancels remaining
           host-call tasks).
        4. ``except* CancelledError`` outside: clear deadline, run
           final pending-jobs drain so JS catch/finally handlers
           execute, inspect the top-level promise state for
           absorption.
        5. Absorption: fulfilled → return value. Rejected with a
           non-HostCancellationError → JS caught the cancel but its
           cleanup threw; surface that. Rejected with
           HostCancellationError or still pending → not absorbed,
           re-raise CancelledError.
        """
        absorbed_handle: Handle | None = None
        cancelled = False
        handle: Handle | None = None
        try:
            try:
                async with asyncio.TaskGroup() as tg:
                    self._active_task_group = tg
                    # Initial eval happens INSIDE the TaskGroup scope
                    # so dispatched host-call tasks become children.
                    handle = get_handle()
                    inner = handle._require_live()
                    # Fast path: non-promise result — no driving.
                    if not inner.is_promise():
                        fast = handle
                        handle = None  # transferred to caller
                        return fast
                    try:
                        while True:
                            # section 7.4 step 1: drain microtasks first.
                            self._engine_ctx.run_pending_jobs()

                            # section 7.4 step 2: check promise state.
                            state = self._engine_ctx.promise_state(inner)

                            # section 7.4 step 3: fulfilled → return result.
                            if state == 1:
                                result_inner = self._engine_ctx.promise_result(inner)
                                result_handle = Handle(self, result_inner)
                                handle.dispose()
                                handle = None
                                return result_handle

                            # section 7.4 step 4: rejected → raise from reason.
                            if state == 2:
                                reason_inner = self._engine_ctx.promise_result(inner)
                                reason_handle = Handle(self, reason_inner)
                                try:
                                    self._raise_from_reason_handle(
                                        reason_handle, deadline
                                    )
                                finally:
                                    reason_handle.dispose()

                            # section 7.4 step 5/6: pending.
                            if not self._pending_tasks:
                                raise DeadlockError(
                                    "eval_async's top-level promise "
                                    "is pending but no async host "
                                    "calls are in flight. Did you "
                                    "forget to register a function "
                                    "as async (is_async=True)? Or is "
                                    "a JS Promise missing its "
                                    "resolver?"
                                )

                            event = self._pending_completed
                            assert event is not None, (
                                "pending task present but completion "
                                "event is None"
                            )
                            await event.wait()
                            event.clear()

                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    "eval_async exceeded its deadline"
                                )
                    except asyncio.CancelledError:
                        cancelled = True
                        self._reject_pending_with_cancellation()
                        raise
            except* asyncio.CancelledError:
                # section 7.4 cancellation step 4-6.
                self._runtime._deadline = None
                self._active_task_group = None
                assert handle is not None
                inner = handle._require_live()
                try:
                    self._engine_ctx.run_pending_jobs()
                    state = self._engine_ctx.promise_state(inner)
                    if state == 1:
                        result_inner = self._engine_ctx.promise_result(inner)
                        absorbed_handle = Handle(self, result_inner)
                        handle.dispose()
                    elif state == 2:
                        reason_inner = self._engine_ctx.promise_result(inner)
                        reason_handle = Handle(self, reason_inner)
                        try:
                            is_our_cancel = self._handle_name_is(
                                reason_handle, "HostCancellationError"
                            )
                            if not is_our_cancel:
                                self._raise_from_reason_handle(
                                    reason_handle, None
                                )
                        finally:
                            reason_handle.dispose()
                        handle.dispose()
                    else:
                        handle.dispose()
                except asyncio.CancelledError:
                    handle.dispose()
                if absorbed_handle is None:
                    raise asyncio.CancelledError() from None
        except BaseExceptionGroup as eg:
            if len(eg.exceptions) == 1:
                inner_exc = eg.exceptions[0]
                if isinstance(inner_exc, BaseException):
                    raise inner_exc from inner_exc.__cause__
            raise
        finally:
            if not cancelled:
                self._active_task_group = None
                if handle is not None:
                    handle.dispose()

        # Reached only on absorption.
        assert absorbed_handle is not None
        return absorbed_handle

    def _raise_from_reason_handle(
        self, reason: Handle, deadline: float | None
    ) -> None:
        """Read name/message/stack off a JS reason Handle, classify
        via _classify_jserror, and raise. Used by the driving loop
        on the rejected-promise path and by the absorption-inspect
        path when JS caught the cancel but its cleanup threw.
        """
        try:
            name = reason._require_live().get("name").to_python()
        except Exception:
            name = "Error"
        try:
            message = reason._require_live().get("message").to_python()
        except Exception:
            message = str(reason._safe_type_of())
        try:
            stack = reason._require_live().get("stack").to_python()
        except Exception:
            stack = None
        if not isinstance(name, str):
            name = "Error"
        if not isinstance(message, str):
            message = str(message) if message is not None else ""
        if stack is not None and not isinstance(stack, str):
            stack = None
        classified = self._classify_jserror(name, message, stack, deadline)
        raise classified from classified.__cause__

    def _handle_name_is(self, reason: Handle, expected: str) -> bool:
        """Read the `.name` property off a JS exception Handle and
        compare to ``expected``. Used by the absorption-inspect path
        to distinguish our injected HostCancellationError from a
        JS-layer re-throw."""
        try:
            got = reason._require_live().get("name").to_python()
        except Exception:
            return False
        return bool(got == expected)

    def _reject_pending_with_cancellation(self) -> None:
        """section 7.4 cancellation step 2: reject every in-flight async
        host-call Promise with a HostCancellationError record.
        Matches the previous implementation pattern — the error's JS-side name is the
        literal "HostCancellationError" (section 10.3)."""
        for pid in list(self._pending_tasks):
            try:
                self._engine_ctx.reject_pending(
                    pid,
                    "HostCancellationError",
                    "eval_async was cancelled",
                    None,
                )
            except Exception:
                # Best-effort: a pid may have been settled between
                # our list() snapshot and the call. Swallow.
                pass

    def _dispatch_async_host_call(
        self, fn_id: int, args: tuple[Any, ...], pending_id: int
    ) -> int:
        """section 6.5 async fn_id → coroutine lookup and scheduling.
        Invoked from the Rust async trampoline. Returns 0 on
        successful scheduling, -1 on failure (the Rust side rejects
        the Promise locally with a HostError in that case).
        """
        fn = self._host_registry.get(fn_id)
        if fn is None:
            return -1
        # Don't re-check iscoroutinefunction here — the user may have
        # passed is_async=True for a callable class whose __call__ is
        # async, which iscoroutinefunction doesn't see. Trust the
        # registration; _run_async_host_call will `await fn(*args)`
        # and produce a clear error if the result isn't awaitable.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running asyncio loop. eval_async creates one; raw
            # sync eval doesn't. The Rust-side sync-eval flag already
            # caught this case; reaching here means a loop disappeared
            # mid-dispatch (shouldn't happen). Fail loudly via -1.
            return -1

        if self._pending_completed is None:
            self._pending_completed = asyncio.Event()

        coro = self._run_async_host_call(fn, args, pending_id)
        if self._active_task_group is not None:
            task = self._active_task_group.create_task(coro)
        else:
            # Fall back to loop.create_task — only reachable from
            # shim-level tests or other callers that dispatch outside
            # eval_async.
            task = loop.create_task(coro)
        self._pending_tasks[pending_id] = task
        return 0

    async def _run_async_host_call(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        pending_id: int,
    ) -> None:
        """Task body for one async host call. On completion, settle
        the JS Promise via resolve_pending / reject_pending, pop from
        the pending-task map, signal the completion event so the
        driving loop can wake.

        The try/finally is load-bearing: a CancelledError arriving
        mid-await raises through the body without reaching the
        explicit settle-and-pop below, which would otherwise leak
        the _pending_tasks entry and break DeadlockError detection.
        """
        resolve_ok = True
        value: Any = None
        err_name = "HostError"
        err_message = ""
        err_stack: str | None = None
        try:
            try:
                value = await fn(*args)
            except asyncio.CancelledError:
                # Cancellation handled by the driving loop: it
                # already called reject_pending with a
                # HostCancellationError before TaskGroup teardown.
                # Propagate to let the task end cleanly.
                raise
            except BaseException as exc:
                resolve_ok = False
                self._last_host_exception = exc
                # Preserve internals in Python (__cause__), but expose
                # only a stable sanitized message over the JS boundary.
                err_message = _HOST_ERROR_SANITIZED_MESSAGE
                err_stack = None

            try:
                if resolve_ok:
                    self._engine_ctx.resolve_pending(pending_id, value)
                else:
                    self._engine_ctx.reject_pending(
                        pending_id, err_name, err_message, err_stack
                    )
            except Exception:
                # Benign: the context may have closed under us, or
                # the pid was already settled by the cancellation
                # walk. section 6.4 tolerates both as no-ops.
                pass
        finally:
            self._pending_tasks.pop(pending_id, None)
            if self._pending_completed is not None:
                self._pending_completed.set()

    def _dispatch_host_call(self, fn_id: int, args: tuple[Any, ...]) -> Any:
        """section 6.5 fn_id → callable lookup and call. Invoked from the
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
                _HOST_ERROR_SANITIZED_MESSAGE,
                None,
            )
        try:
            return fn(*args)
        except BaseException as exc:
            self._last_host_exception = exc
            # Preserve original exception in __cause__, while
            # sanitizing the JS-visible HostError payload.
            raise _engine.JSError(
                "HostError", _HOST_ERROR_SANITIZED_MESSAGE, None
            ) from None
