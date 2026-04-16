"""Context. See spec/implementation.md §7.2."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, overload

import wasmtime

from quickjs_wasm import _msgpack
from quickjs_wasm._msgpack import Undefined
from quickjs_wasm.errors import (
    ConcurrentEvalError,
    DeadlockError,
    JSError,
    MarshalError,
    QuickJSError,
    TimeoutError,
)
from quickjs_wasm.globals import Globals

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from quickjs_wasm.handle import Handle
    from quickjs_wasm.runtime import Runtime


def _detect_is_async(fn: Callable[..., Any]) -> bool:
    """§7.4: infer async-ness of a registered host function.

    Uses ``inspect.iscoroutinefunction`` directly. If it says False but
    the ``__wrapped__`` chain reveals a coroutine function underneath
    (the common case of a decorator that didn't preserve the coroutine
    marker), we raise ``TypeError`` rather than silently register as
    sync — the latter fails at runtime in confusing ways. The user
    gets an explicit-override instruction in the error message.

    Anything else (C extensions, objects with ``__call__``, partials
    of partials of coroutines beyond the ``__wrapped__`` chain) falls
    through to whatever ``iscoroutinefunction`` reports; if that's
    wrong, ``is_async=True/False`` is the escape hatch.
    """
    if inspect.iscoroutinefunction(fn):
        return True
    # Follow the __wrapped__ chain (functools.wraps populates this).
    # Cap iterations to avoid cycles; five is plenty for the
    # decorator stacks we'd reasonably encounter.
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
                "__wrapped__ chain contains one. This usually means a "
                "decorator dropped the coroutine marker. Pass "
                "is_async=True explicitly to ctx.register / "
                "@ctx.function(is_async=True)."
            )
    return False


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
        # §7.4 / §7.3: cumulative timeout budget for eval_async. Starts
        # counting from context creation. Per-call timeout= on
        # eval_async overrides for the duration of that call.
        self._cumulative_deadline = time.monotonic() + timeout
        # §7.4 concurrency rule: only one eval_async in flight per
        # context. Set on entry, cleared on exit via try/finally.
        self._eval_async_in_flight = False

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
            # Non-timeout trap — most commonly wasm-level stack
            # exhaustion from a deep JS recursion chain that outran
            # QuickJS's own JS_CHECK_STACK_OVERFLOW. Log the raw trap
            # so future debugging of "weird trap in the wild" has a
            # breadcrumb beyond the synthesized JSError message.
            _log.debug("non-timeout wasm trap during eval: %s", trap)
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
        """Thin passthrough to Bridge.raise_from_exception_slot.

        Kept as a method on Context for callers (Handle, mostly) that
        already hold a Context reference but not a Bridge — one less
        attribute hop. All exception routing lives in Bridge to keep
        §10.1 / §10.2 logic in one place.
        """
        self._bridge.raise_from_exception_slot(self._ctx_id, exc_slot)

    def eval_handle(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> Handle:
        if self._closed:
            raise QuickJSError("context is closed")
        del filename
        flags = 0
        if module:
            flags |= 0x1
        if strict:
            flags |= 0x4
        self._bridge.take_last_host_exception()
        deadline = time.monotonic() + self._timeout
        self._bridge.set_deadline(deadline)
        try:
            status, slot = self._bridge.eval(self._ctx_id, code, flags)
        except wasmtime.Trap as trap:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"JS evaluation exceeded {self._timeout}s "
                    f"(epoch trap: {trap})"
                ) from None
            _log.debug("non-timeout wasm trap during eval_handle: %s", trap)
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
        from quickjs_wasm.handle import Handle as _Handle
        return _Handle(self, self._bridge, self._ctx_id, slot)

    # ---- Async API (§7.4) ---------------------------------------------

    async def eval_async(
        self,
        code: str,
        *,
        module: bool = True,
        strict: bool = False,
        filename: str = "<eval>",
        timeout: float | None = None,
    ) -> Any:
        """Evaluate code with top-level await + async host-call support.

        See §7.4 for the full execution model. Defaults to module mode
        so top-level await works.

        Cancellation: if the enclosing asyncio task is cancelled, the
        driving loop rejects in-flight host-call Promises with a
        HostCancellationError and lets JS ``catch``/``finally``
        handlers run. If JS absorbs the cancellation (catches and
        returns normally), eval_async returns the fulfilled value
        without re-raising asyncio.CancelledError. Callers who need
        cancellation to always propagate regardless of JS absorption
        can check ``asyncio.current_task().cancelling() > 0`` after
        the call — the cancellation counter is set by
        ``task.cancel()`` and not cleared by our absorption path.
        """
        settled_slot = await self._eval_and_drive(
            code, module=module, strict=strict, filename=filename, timeout=timeout
        )
        try:
            mp_status, payload = self._bridge.to_msgpack(
                self._ctx_id, settled_slot
            )
            if mp_status < 0:
                raise MarshalError(
                    "eval_async result is not marshalable; use "
                    "eval_handle_async to keep the value as a Handle"
                )
            value = _msgpack.decode(payload)
            if isinstance(value, Undefined) and not self.preserve_undefined:
                return None
            return value
        finally:
            self._bridge.slot_drop(self._ctx_id, settled_slot)

    async def eval_handle_async(
        self,
        code: str,
        *,
        module: bool = True,
        strict: bool = False,
        filename: str = "<eval>",
        timeout: float | None = None,
    ) -> Handle:
        settled_slot = await self._eval_and_drive(
            code, module=module, strict=strict, filename=filename, timeout=timeout
        )
        from quickjs_wasm.handle import Handle as _Handle
        return _Handle(self, self._bridge, self._ctx_id, settled_slot)

    async def _eval_and_drive(
        self,
        code: str,
        *,
        module: bool,
        strict: bool,
        filename: str,
        timeout: float | None,
    ) -> int:
        """Shared prologue + eval + driving loop for the two async
        entry points. Returns a slot owning the settled value; caller
        disposes or wraps it. Raises on JS exception via the standard
        §10.1 routing.
        """
        if self._closed:
            raise QuickJSError("context is closed")
        # §7.4 concurrency rule. Check BEFORE touching the shim so a
        # concurrent violation is cheap and leaves no side effects.
        if self._eval_async_in_flight:
            raise ConcurrentEvalError(
                "another eval_async is already in flight on this context; "
                "use a separate context for concurrent JS workloads"
            )
        del filename
        # §7.2 / §7.4: module=True means "top-level await enabled" at
        # the user level. Under the hood that's script-mode (bit 0
        # clear) plus the async flag (bit 3). module=False is plain
        # script mode. True ES module mode (bit 0 set) is not exposed
        # through eval_async because quickjs-ng's top-level-await
        # support is script-mode-only.
        flags = 0
        if module:
            flags |= 0x8  # async flag
        if strict:
            flags |= 0x4

        self._eval_async_in_flight = True
        self._bridge.take_last_host_exception()
        # §7.4 timeout semantics. Per-call timeout= overrides the
        # cumulative budget for the duration of this call only.
        if timeout is not None:
            deadline = time.monotonic() + timeout
        else:
            deadline = self._cumulative_deadline
        self._bridge.set_deadline(deadline)
        try:
            try:
                status, slot = self._bridge.eval(self._ctx_id, code, flags)
            except wasmtime.Trap as trap:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"JS evaluation exceeded timeout "
                        f"(epoch trap: {trap})"
                    ) from None
                _log.debug("non-timeout wasm trap during eval_async: %s", trap)
                raise JSError(
                    "InternalError",
                    f"wasm trap during JS evaluation: {trap}",
                    None,
                ) from None
            if status < 0:
                raise QuickJSError(f"shim error from qjs_eval: status={status}")
            if status == 1:
                try:
                    self._raise_from_exception_slot(slot)
                finally:
                    self._bridge.slot_drop(self._ctx_id, slot)
            settled_slot = await self._drive_promise(slot, deadline)
            # §7.4 / quickjs-ng async-eval envelope: when bit 3 (async)
            # was set, the resolved value is wrapped as {value: x, done}
            # (iterator-result shape). Unwrap the `value` property before
            # handing back to the caller. module=False doesn't set bit 3
            # and returns the raw expression result, so only unwrap when
            # we know we asked for the async mode.
            if module:
                try:
                    status_g, value_slot = self._bridge.get_prop(
                        self._ctx_id, settled_slot, "value"
                    )
                    if status_g != 0:
                        raise QuickJSError(
                            "unable to unwrap async-eval {value: x} envelope"
                        )
                finally:
                    self._bridge.slot_drop(self._ctx_id, settled_slot)
                return value_slot
            return settled_slot
        finally:
            self._bridge.set_deadline(None)
            self._eval_async_in_flight = False

    async def _drive_promise(self, slot: int, deadline: float) -> int:
        """§7.4 driving loop. Consumes ``slot`` (drops on any exit path
        that doesn't return it); returns a new slot owning the settled
        value.

        Fast path: if ``slot`` is not a promise, it's already settled.
        Pure-sync code like ``eval_async("1 + 2")`` skips the loop and
        marshals directly.

        Cancellation: the loop is wrapped in a TaskGroup that owns all
        in-flight host-call tasks (dispatcher checks
        ``bridge._active_task_group``). When the outer task is
        cancelled:

        1. Driving loop's ``event.wait()`` raises CancelledError.
        2. We reject every in-flight Promise with a HostCancellationError
           record via ``qjs_promise_reject`` — this must happen BEFORE
           TaskGroup teardown so the JS-side rejection sees live
           resolver callables.
        3. Re-raise CancelledError to tear down the TaskGroup (cancels
           remaining host-call tasks).
        4. ``except* CancelledError`` outside the TaskGroup: clear the
           deadline, run final pending-jobs drain so JS catch/finally
           handlers execute, inspect the top-level promise state for
           absorption detection.
        5. Absorption: if the top-level promise is now fulfilled, JS
           caught HostCancellationError and recovered — return the
           fulfilled value normally. If rejected with a non-
           HostCancellationError, JS caught the cancellation but its
           cleanup itself threw — surface that exception. If still
           rejected with HostCancellationError (the common case: no
           catch handler), re-raise CancelledError.
        """
        # Fast path: non-promise results don't need the driving loop.
        if not self._bridge.is_promise(self._ctx_id, slot):
            return slot

        # From here, `slot` holds the top-level promise.
        #
        # The loop runs inside a TaskGroup so cancellation cascades to
        # in-flight host-call tasks. On cancellation we reject every
        # in-flight Promise with a HostCancellationError record, let
        # the TaskGroup tear down, then inspect the top-level promise
        # state for absorption detection.
        #
        # `absorbed_result_slot` threads a fulfilled value out of the
        # absorption path — we can't `return` from inside an except*
        # block (Python 3.11 restriction), so the cancellation handler
        # sets this variable and the return happens after the block.
        absorbed_result_slot: int | None = None
        cancelled = False
        # Nested try so we can distinguish "CancelledError bubbled out
        # of the TaskGroup" (handled via except* for absorption) from
        # "a regular exception bubbled out" (DeadlockError, TimeoutError,
        # JSError, HostError — re-raised unwrapped). TaskGroup wraps
        # everything in BaseExceptionGroup on exit; the inner except*
        # strips the cancel subgroup, and the outer except unwraps a
        # single-exception group into its sole child so callers see a
        # bare DeadlockError etc. rather than BaseExceptionGroup[...].
        try:
            try:
                async with asyncio.TaskGroup() as tg:
                    self._bridge._active_task_group = tg
                    try:
                        while True:
                            # §7.4 step 1: drain microtasks first.
                            # Drain-then-check, not check-then-drain:
                            # a just-completed host call may have
                            # queued a microtask that settles the
                            # top-level promise.
                            rc, _count = self._bridge.runtime_run_pending_jobs(
                                self._runtime._rt_id
                            )
                            if rc < 0:
                                raise QuickJSError(
                                    f"shim error from runtime_run_pending_jobs: {rc}"
                                )

                            # §7.4 step 2: check promise state.
                            state = self._bridge.promise_state(
                                self._ctx_id, slot
                            )

                            # §7.4 step 3: fulfilled → return result.
                            if state == 1:
                                _, result_slot = self._bridge.promise_result(
                                    self._ctx_id, slot
                                )
                                self._bridge.set_deadline(None)
                                self._bridge.slot_drop(self._ctx_id, slot)
                                return result_slot

                            # §7.4 step 4: rejected → raise from reason.
                            if state == 2:
                                _, reason_slot = self._bridge.promise_result(
                                    self._ctx_id, slot
                                )
                                try:
                                    self._raise_from_exception_slot(
                                        reason_slot
                                    )
                                finally:
                                    self._bridge.slot_drop(
                                        self._ctx_id, reason_slot
                                    )

                            # §7.4 step 5 / 6: pending.
                            if not self._bridge._pending_tasks:
                                raise DeadlockError(
                                    "eval_async's top-level promise is "
                                    "pending but no async host calls "
                                    "are in flight. Did you forget to "
                                    "register a function as async "
                                    "(is_async=True)? Or is a JS "
                                    "Promise missing its resolver?"
                                )

                            # §7.4 step 5: wait for next host-call
                            # completion. Single-waiter invariant: the
                            # concurrent-eval guard in _eval_and_drive
                            # keeps this loop as the sole consumer of
                            # the completion event.
                            event = self._bridge._pending_completed
                            assert event is not None, (
                                "pending task present but completion "
                                "event is None; indicates a race in "
                                "_host_call_async_dispatch"
                            )
                            # Wait/clear ordering: wait first, clear,
                            # loop. Clear-first would drop a set()
                            # fired between clear and wait; no clear
                            # would busy-spin.
                            await event.wait()
                            event.clear()

                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    "eval_async exceeded its deadline"
                                )
                    except asyncio.CancelledError:
                        # §7.4 cancellation step 2-3: reject every
                        # in-flight Promise with a HostCancellationError
                        # record BEFORE TaskGroup teardown so the
                        # rejections fire against live JS resolvers.
                        cancelled = True
                        self._reject_pending_with_cancellation()
                        raise  # triggers TaskGroup teardown
            except* asyncio.CancelledError:
                # §7.4 cancellation step 4-6: TaskGroup is exited; all
                # child tasks cancelled. Clear deadline (§6.4) before
                # the final drain, then inspect promise state for
                # absorption.
                self._bridge.set_deadline(None)
                self._bridge._active_task_group = None
                try:
                    self._bridge.runtime_run_pending_jobs(
                        self._runtime._rt_id
                    )
                    state = self._bridge.promise_state(
                        self._ctx_id, slot
                    )
                    if state == 1:
                        # §7.4 step 5: JS absorbed cancellation. Hold
                        # the result so the post-except* return can
                        # hand it back to the caller.
                        _, absorbed_result_slot = (
                            self._bridge.promise_result(self._ctx_id, slot)
                        )
                        self._bridge.slot_drop(self._ctx_id, slot)
                    elif state == 2:
                        _, reason_slot = self._bridge.promise_result(
                            self._ctx_id, slot
                        )
                        # Distinguish "rejected with our injected
                        # HostCancellationError" (common case: no JS
                        # catch) from "rejected with something else"
                        # (JS caught but its cleanup threw).
                        is_our_cancel = self._exception_name_is(
                            reason_slot, "HostCancellationError"
                        )
                        if not is_our_cancel:
                            try:
                                self._raise_from_exception_slot(
                                    reason_slot
                                )
                            finally:
                                self._bridge.slot_drop(
                                    self._ctx_id, reason_slot
                                )
                        self._bridge.slot_drop(
                            self._ctx_id, reason_slot
                        )
                        self._bridge.slot_drop(self._ctx_id, slot)
                    else:
                        # Still pending after final drain — shouldn't
                        # happen; defensively drop and re-raise below.
                        self._bridge.slot_drop(self._ctx_id, slot)
                except asyncio.CancelledError:
                    # The absorption-inspection path itself got
                    # cancelled. Fall through to re-raise.
                    self._bridge.slot_drop(self._ctx_id, slot)
                # If absorption succeeded, absorbed_result_slot is
                # set; fall through to the return at the bottom.
                # Otherwise re-raise CancelledError.
                if absorbed_result_slot is None:
                    # Raised inside except*; from None suppresses the
                    # ExceptionGroup as __context__ since the group is
                    # an implementation detail of the TaskGroup, not
                    # information the caller should see.
                    raise asyncio.CancelledError() from None
        except BaseExceptionGroup as eg:
            # Non-cancellation exception bubbled out of the TaskGroup.
            # TaskGroup wraps everything in BaseExceptionGroup on exit;
            # we already peeled off the CancelledError subgroup above.
            # Anything here is a user-visible error (DeadlockError,
            # TimeoutError, JSError, HostError).
            if len(eg.exceptions) == 1:
                inner = eg.exceptions[0]
                if isinstance(inner, BaseException):
                    # Preserve inner.__cause__ (set by
                    # _raise_from_exception_slot via `raise err from
                    # cause` for HostError). A bare `raise inner`
                    # triggers B904; `raise inner from inner.__cause__`
                    # is a semantic no-op that preserves the chain
                    # and satisfies the linter. §10.5 documents the
                    # idiom. Users of eval_async see a bare
                    # DeadlockError / HostError / etc. as if the
                    # TaskGroup were invisible.
                    raise inner from inner.__cause__
            # Multi-exception group: defensive path. Empirically
            # unreachable under the current driving-loop structure
            # (the loop drains jobs between waits, any rejection
            # exits step 4 immediately, cancelled siblings surface
            # as CancelledError and are peeled off by the except*
            # above). _run_async_host_call catches all non-cancel
            # exceptions internally and encodes them as
            # promise_reject payloads, so host tasks don't raise
            # into the group in normal flow. Reaching here would
            # require promise_reject itself to throw (shim-boundary
            # failure) or a future refactor that lets host tasks
            # raise bare Python exceptions into the group. If this
            # ever fires, the right fix is to unwrap and surface the
            # first exception with the rest as __context__, or to
            # define a multi-HostError surface. For now, pass through
            # the BaseExceptionGroup so the failure is visible rather
            # than silently wrong.
            raise
        finally:
            # Final non-cancellation cleanup. The cancellation path
            # does its own set_deadline(None) + slot_drop inside the
            # except*; redoing here for that path would be benign
            # (slot_drop on a freed slot is a shim-level no-op per §6.4).
            if not cancelled:
                self._bridge.set_deadline(None)
                self._bridge._active_task_group = None
                self._bridge.slot_drop(self._ctx_id, slot)

        # Reached only on absorption: JS caught HostCancellationError
        # and the promise fulfilled with a recovery value.
        return absorbed_result_slot

    def _reject_pending_with_cancellation(self) -> None:
        """§7.4 cancellation step 2: reject every in-flight async
        host-call Promise with a HostCancellationError record. Matches
        the shim's string-literal injection convention (§10.3)."""
        record = {
            "name": "HostCancellationError",
            "message": "eval_async was cancelled",
            "stack": None,
        }
        for pid in list(self._bridge._pending_tasks):
            try:
                self._bridge.promise_reject(self._ctx_id, pid, record)
            except Exception:  # noqa: BLE001 — best-effort cancellation
                # Individual settlement failures shouldn't block the
                # rest. The common case is pid already settled
                # concurrently by a task that finished between our
                # loop iteration and the CancelledError arriving —
                # shim returns -1 there, which surfaces as a bridge
                # RuntimeError; swallow.
                pass

    def _exception_name_is(self, exc_slot: int, expected_name: str) -> bool:
        """Read the ``.name`` property off a JS exception slot and
        compare to ``expected_name``. Used to distinguish the shim-
        injected HostCancellationError rejection from a user-level
        rejection produced by JS catch-handlers that re-threw."""
        status, name_slot = self._bridge.get_prop(
            self._ctx_id, exc_slot, "name"
        )
        if status != 0 or name_slot == 0:
            if name_slot:
                self._bridge.slot_drop(self._ctx_id, name_slot)
            return False
        try:
            mp_status, payload = self._bridge.to_msgpack(
                self._ctx_id, name_slot
            )
            if mp_status < 0:
                return False
            return bool(_msgpack.decode(payload) == expected_name)
        finally:
            self._bridge.slot_drop(self._ctx_id, name_slot)

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

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        is_async: bool | None = None,
    ) -> None:
        """Register a Python callable as a JS global function. §7.2, §7.4.

        ``is_async``:

        - ``None`` (default) — auto-detect via
          ``inspect.iscoroutinefunction(fn)``. If ``fn`` is a
          coroutine function, it's registered as async and JS-side
          calls return a Promise. Otherwise sync.
        - ``True`` / ``False`` — explicit override, for callables
          where auto-detection can't see through a wrapper.

        If auto-detection says sync but the callable's
        ``__wrapped__`` chain reveals a coroutine underneath, the
        detection can't be trusted and we raise ``TypeError`` rather
        than silently registering sync. That's the common source of
        detection failures (decorators that forget to preserve the
        coroutine marker), and the right fix is ``is_async=True``
        on the registration — not heuristic guessing.
        """
        if self._closed:
            raise QuickJSError("context is closed")
        if is_async is None:
            is_async_resolved = _detect_is_async(fn)
        else:
            is_async_resolved = is_async
        self._bridge.register_host_function(
            self._ctx_id, name, fn, is_async=is_async_resolved
        )

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._timeout = value
