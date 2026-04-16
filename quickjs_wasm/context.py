"""Context. See spec/implementation.md §7.2."""

from __future__ import annotations

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
        """See §7.4. Defaults to module mode so top-level await works."""
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
        """
        # Fast path: non-promise results don't need the driving loop.
        if not self._bridge.is_promise(self._ctx_id, slot):
            return slot

        # From here, `slot` holds the top-level promise. The loop owns
        # its lifetime; the finally block drops it on any exit (return
        # or raise). The returned result_slot on the fulfilled path is
        # a fresh allocation from promise_result, so dropping the
        # outer promise slot is always safe.
        try:
            while True:
                # §7.4 step 1: drain microtasks first.
                # Ordering matters: drain-then-check, not check-then-drain.
                # A just-completed host call may have queued a microtask
                # that settles the top-level promise; running jobs first
                # lets state transitions propagate before we inspect.
                rc, _count = self._bridge.runtime_run_pending_jobs(
                    self._runtime._rt_id
                )
                if rc < 0:
                    raise QuickJSError(
                        f"shim error from runtime_run_pending_jobs: {rc}"
                    )
                # rc == 1 means a microtask raised. The exception stays
                # on the context; it'll surface via the promise's
                # rejected state on the next iteration or via the
                # next state check below.

                # §7.4 step 2: check promise state.
                state = self._bridge.promise_state(self._ctx_id, slot)

                # §7.4 step 3: fulfilled → marshal and return.
                if state == 1:
                    _, result_slot = self._bridge.promise_result(
                        self._ctx_id, slot
                    )
                    return result_slot

                # §7.4 step 4: rejected → extract reason and raise.
                if state == 2:
                    _, reason_slot = self._bridge.promise_result(
                        self._ctx_id, slot
                    )
                    try:
                        self._raise_from_exception_slot(reason_slot)
                    finally:
                        self._bridge.slot_drop(self._ctx_id, reason_slot)

                # §7.4 step 5 / step 6: pending. Dispatch based on
                # whether any async host calls are in flight.
                if not self._bridge._pending_tasks:
                    # §7.4 step 6: nothing will settle this promise.
                    # Actionable error message — users in this loop
                    # will read it first when debugging.
                    raise DeadlockError(
                        "eval_async's top-level promise is pending but "
                        "no async host calls are in flight. Did you "
                        "forget to register a function as async "
                        "(is_async=True)? Or is a JS Promise missing "
                        "its resolver?"
                    )

                # §7.4 step 5: wait for the next host-call completion.
                # The Event was lazy-created by the first dispatch; by
                # the time we reach here with _pending_tasks non-empty,
                # it exists. Single-waiter invariant: the driving loop
                # is the only consumer of this Event. Multi-waiter
                # semantics (debuggers, monitoring) would require an
                # asyncio.Condition or per-dispatch futures — revisit
                # in v0.3+ (see step-3 flag in v0.2 plan).
                event = self._bridge._pending_completed
                assert event is not None, (
                    "pending task present but completion event is None; "
                    "indicates a race in _host_call_async_dispatch"
                )
                # Wait/clear ordering: wait first, then clear, then
                # loop back to drain + re-check. If we cleared first,
                # a set() that fires between clear and wait would be
                # lost. If we forgot to clear, the next wait returns
                # immediately and the loop busy-spins.
                await event.wait()
                event.clear()
                # Timeout enforcement: host_interrupt fires against
                # the deadline we set earlier, so QuickJS itself will
                # abort the next job-drain with an interrupted error.
                # But a task that's blocked in Python-land awaiting
                # something outside the shim (e.g. asyncio.sleep)
                # won't see the interrupt. Check here as a belt-and-
                # suspenders deadline tripwire for the async path.
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "eval_async exceeded its deadline"
                    )
        finally:
            # Clear the epoch deadline BEFORE cleanup calls enter wasm
            # — otherwise a deadline that just elapsed (the very thing
            # that triggered this finally) would trap slot_drop. The
            # outer caller re-sets the deadline for any subsequent
            # operation; for cleanup we want the unbounded deadline.
            self._bridge.set_deadline(None)
            # Always drop the promise's own slot. On the happy path
            # we return result_slot (a fresh allocation from
            # promise_result); on error paths we drop any reason_slot
            # inline above. The outer promise slot is always ours to
            # clean up here.
            self._bridge.slot_drop(self._ctx_id, slot)

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
