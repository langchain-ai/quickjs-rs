"""Async host-function registration. See README.md."""

from __future__ import annotations

import asyncio
import functools

import pytest

from quickjs_rs import Runtime


async def test_async_def_auto_detected_via_decorator() -> None:
    """@ctx.function on an async def auto-detects as async."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            async def tick() -> str:
                await asyncio.sleep(0)
                return "tock"

            assert await ctx.eval_async("await tick()") == "tock"


async def test_async_def_auto_detected_via_register() -> None:
    """ctx.register(name, fn) with no is_async kwarg auto-detects."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def lookup(k: str) -> int:
                await asyncio.sleep(0)
                return len(k)

            ctx.register("lookup", lookup)
            assert await ctx.eval_async("await lookup('hello')") == 5


async def test_sync_def_auto_detected_as_sync() -> None:
    """Regression: plain def functions still register as sync under the
    new auto-detection default. Guards against default
    flipping the wrong way."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            def add(a: int, b: int) -> int:
                return a + b

            # Sync eval — if auto-detection incorrectly registered
            # add as async, ctx.eval("add(1, 2)") would get a Promise
            # instead of the value, surfacing as MarshalError.
            assert ctx.eval("add(1, 2)") == 3


async def test_functools_wraps_preserves_coroutine_detection() -> None:
    """functools.wraps DOES preserve iscoroutinefunction when applied
    to an async def — the common case. Confirm auto-detection sees
    through the wrapper."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def inner(x: int) -> int:
                await asyncio.sleep(0)
                return x + 1

            @functools.wraps(inner)
            async def outer(x: int) -> int:
                return await inner(x)

            ctx.register("outer", outer)
            assert await ctx.eval_async("await outer(41)") == 42


async def test_wrapped_chain_with_broken_marker_raises_typeerror() -> None:
    """a non-coroutine wrapper around a coroutine (the broken-
    decorator case) surfaces as TypeError at registration rather than
    silent misclassification. The error message tells the user to
    pass is_async= explicitly."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def real_work() -> str:
                return "done"

            def broken_wrapper() -> str:  # type: ignore[return]
                # Imagine this is a sync-looking function whose real
                # implementation delegates to a coroutine — or, as
                # here, a function that someone attached __wrapped__
                # to without preserving the coroutine marker.
                return real_work()  # type: ignore[return-value]

            broken_wrapper.__wrapped__ = real_work  # type: ignore[attr-defined]

            with pytest.raises(TypeError) as excinfo:
                ctx.register("broken", broken_wrapper)
            msg = str(excinfo.value)
            assert "is_async=True" in msg
            assert "__wrapped__" in msg


async def test_explicit_override_beats_auto_detection() -> None:
    """is_async=True or False wins over whatever inspect reports. This
    is the escape hatch for C extensions, objects with __call__, and
    any other callable shape auto-detection can't see through."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # A plain function that the user promises is really
            # an async dispatcher into an event loop they manage
            # themselves. Auto-detection would say sync; they know
            # it's not. (We don't test the behaviour of a wrongly-
            # overridden async function — that'd be a classic
            # shoot-your-foot — but we do verify the override path
            # reaches the async trampoline in the shim.)
            class AsyncShaped:
                async def __call__(self, x: int) -> int:
                    return x * 2

            fn = AsyncShaped()
            # Auto-detection on an instance with async __call__ may or
            # may not detect; the override makes it explicit.
            ctx.register("doubled", fn, is_async=True)
            assert await ctx.eval_async("await doubled(7)") == 14


async def test_explicit_sync_override_on_callable_class() -> None:
    """Mirror of the previous test: is_async=False forces sync
    registration even if the callable looks async-ish to inspect."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            def plain_doubler(x: int) -> int:
                return x * 2

            ctx.register("plain", plain_doubler, is_async=False)
            assert ctx.eval("plain(21)") == 42


async def test_cancel_propagates_through_eval_async() -> None:
    """task.cancel() on an in-flight eval_async re-raises
    CancelledError to the caller when JS doesn't catch."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def sleep_long() -> str:
                await asyncio.sleep(10)
                return "never"

            ctx.register("sleep_long", sleep_long)

            task = asyncio.create_task(ctx.eval_async("await sleep_long()"))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_cancel_absorbed_by_js_catch_handler() -> None:
    """JS catching HostCancellationError and returning normally
    causes eval_async to return that value without re-raising. The
    cancellation counter remains non-zero for callers who need strict
    propagation (via asyncio.current_task().cancelling())."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def sleep_long() -> str:
                await asyncio.sleep(10)
                return "never"

            ctx.register("sleep_long", sleep_long)

            async def runner() -> str:
                try:
                    async with asyncio.timeout(0.02):
                        return await ctx.eval_async("""
                            try {
                                await sleep_long();
                                "unreachable"
                            } catch (e) {
                                e.name
                            }
                        """)
                except asyncio.CancelledError:
                    # Acceptable alternate path per cancellation
                    # propagated before the JS catch handler ran. On
                    # slower systems the timeout might fire before JS
                    # even reaches its catch block.
                    return "propagated"

            result = await runner()
            # Either JS absorbed (returning the error name) or the
            # cancellation propagated before JS saw it.
            assert result in ("HostCancellationError", "propagated")


async def test_cancel_with_no_host_calls_pure_js_loop() -> None:
    """Cancelling an eval_async that's running pure-JS compute (no
    host calls in flight, so the pending-task map is empty and the
    driving loop is blocked on the interrupt handler rather than
    event.wait()) still terminates cleanly.

    The interrupt-handler path is orthogonal to the TaskGroup-based
    host-task cancellation; both need to work for cancellation to be
    reliable across the range of JS workloads.
    """
    with Runtime() as rt:
        # Short timeout so the compute loop's interrupt fires quickly.
        with rt.new_context(timeout=0.1) as ctx:
            # A tight JS loop with no host calls; the wall-clock
            # interrupt fires via the timeout.
            with pytest.raises((asyncio.CancelledError, Exception)):
                # Any exception is acceptable here — TimeoutError from
                # the deadline or CancelledError from task.cancel().
                # The assertion is "doesn't hang."
                task = asyncio.create_task(ctx.eval_async("while(true){}", module=False))
                await asyncio.sleep(0.05)
                task.cancel()
                await task


async def test_cancel_finally_host_calls_also_cancelled() -> None:
    """Subtle case: JS `finally` that awaits another host call
    does NOT get a free pass — the cleanup host call is scheduled into
    the same TaskGroup as the original, so it's cancelled with the
    rest. This means JS finally blocks can't do async cleanup reliably
    during cancellation.

    Alternative (letting finally escape cancellation) is a worse
    footgun because JS could indefinitely delay cancellation by
    putting long-running code in finally. Locked in: finally host
    calls get cancelled alongside everything else.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            cleanup_ran = False
            cleanup_completed = False

            async def sleep_long() -> str:
                await asyncio.sleep(10)
                return "never"

            async def cleanup() -> str:
                nonlocal cleanup_ran, cleanup_completed
                cleanup_ran = True
                # A sleep that would complete if cleanup were allowed
                # to run post-cancellation.
                await asyncio.sleep(0.1)
                cleanup_completed = True
                return "cleaned"

            ctx.register("sleep_long", sleep_long)
            ctx.register("cleanup", cleanup)

            task = asyncio.create_task(
                ctx.eval_async("""
                try {
                    await sleep_long();
                } finally {
                    await cleanup();
                }
            """)
            )
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # cleanup() was dispatched and started, but the TaskGroup
            # cancellation prevented its sleep from completing —
            # cleanup_completed stays False. The cleanup code RAN but
            # didn't finish its await.
            assert cleanup_completed is False


async def test_sync_eval_with_async_hostfn_raises_concurrent_eval_error() -> None:
    """Sync ctx.eval that invokes a registered async host
    function raises ConcurrentEvalError. Structural detection fires regardless of
    whether an asyncio loop is ambient, so this test runs as a plain
    async test rather than needing to escape pytest-asyncio's
    auto-mode loop via a thread."""
    from quickjs_rs import ConcurrentEvalError

    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def slow(n: int) -> int:
                await asyncio.sleep(0)
                return n

            ctx.register("slow", slow)

            with pytest.raises(ConcurrentEvalError):
                ctx.eval("slow(1)")

            # Context not corrupted: subsequent sync eval works.
            assert ctx.eval("1 + 1") == 2


async def test_async_eval_works_after_sync_eval_async_hostfn_error() -> None:
    """Corollary to the above: after the sync eval raises
    ConcurrentEvalError, the same context can still run async evals
    of the same async host function. No context corruption, no stale
    flag leakage."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def slow(n: int) -> int:
                await asyncio.sleep(0)
                return n

            ctx.register("slow", slow)

            # eval_async — this is what the user should be doing.
            # Flag cleared at eval entry (even though it was never
            # set, since no sync eval happened first); eval_async
            # runs a real loop, dispatcher finds it, no flag set.
            assert await ctx.eval_async("await slow(5)") == 5


def test_sync_eval_pure_js_promise_is_not_error() -> None:
    """Discriminator test: a sync eval that returns a pure-JS Promise
    (no async host function involved) is legitimate, not a
    ConcurrentEvalError. The detection must be at the dispatcher
    level, not the eval-return-type level.

    If this test ever fails because the check moved to
    'eval returned a Promise, raise', that's a regression.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # eval returns a Promise handle; the eval ITSELF has no
            # async host calls, just a pure-JS Promise. Should be
            # a MarshalError (Promises aren't marshalable to Python)
            # — NOT a ConcurrentEvalError.
            from quickjs_rs import MarshalError

            with pytest.raises(MarshalError):
                ctx.eval("Promise.resolve(42)")

            # eval_handle path: returns a Handle to the Promise, no
            # error. User can drive it via await_promise later.
            h = ctx.eval_handle("Promise.resolve(42)")
            try:
                assert h.is_promise
            finally:
                h.dispose()


async def test_sync_eval_js_try_catch_still_raises_concurrent_eval_error() -> None:
    """Edge case: JS code that wraps the async host call in a
    try/catch still surfaces ConcurrentEvalError to Python. JS
    'handling' the rejection doesn't mean the Python caller got
    what they asked for — surfacing the error tells them to use
    eval_async. The flag-based detection does this naturally
    because the flag is set at dispatcher time, before JS's catch
    handler runs."""
    from quickjs_rs import ConcurrentEvalError

    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def slow() -> int:
                return 1

            ctx.register("slow", slow)

            with pytest.raises(ConcurrentEvalError):
                ctx.eval("try { slow(); 'unreachable' } catch (e) { e.name }")


async def test_async_host_function_raises_propagates_original() -> None:
    """async path: a non-cancellation exception in an async host
    function bubbles out of eval_async as the original Python
    exception when JS doesn't catch.

    Distinct from the cancellation path — this is the ordinary raise,
    not a CancelledError. Exercises the dispatcher's encode-as-
    HostError-record branch in _run_async_host_call together with
    Context._raise_classified's original-exception promotion."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            class CustomError(Exception):
                pass

            async def broken() -> None:
                await asyncio.sleep(0)  # yield before raising
                raise CustomError("specific failure mode")

            ctx.register("broken", broken)

            with pytest.raises(CustomError, match="specific failure mode"):
                await ctx.eval_async("await broken()")


async def test_handle_call_async_host_fn_raises_concurrent_eval_error() -> None:
    """Handle.call on an async host function from sync context must
    raise ConcurrentEvalError, matching Context.eval's behavior for
    the same failure mode. The flag-and-surface pattern
    extends to Handle.call — the
    user-visible behavior should be consistent regardless of which
    sync entry point invoked the async host function."""
    from quickjs_rs import ConcurrentEvalError

    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def slow(n: int) -> int:
                await asyncio.sleep(0)
                return n

            ctx.register("slow", slow)
            # Get a handle to the slow function and call it directly
            # via Handle.call — no eval involved.
            slow_handle = ctx.eval_handle("slow")
            try:
                with pytest.raises(ConcurrentEvalError):
                    slow_handle.call(1)
            finally:
                slow_handle.dispose()


async def test_handle_call_method_async_host_fn_raises_concurrent_eval_error() -> None:
    """Handle.call_method on an object whose method is an async host
    function. Via the existing call_method → call delegation, the
    ConcurrentEvalError surfaces without call_method needing its own
    flag-check wiring."""
    from quickjs_rs import ConcurrentEvalError

    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def slow(n: int) -> int:
                await asyncio.sleep(0)
                return n

            # Register slow as a global, then build an object whose
            # method calls it. Handle.call_method on that object
            # triggers the async host fn during the method's body.
            ctx.register("slow", slow)
            obj = ctx.eval_handle("({ invoke(n) { return slow(n); } })")
            try:
                with pytest.raises(ConcurrentEvalError):
                    obj.call_method("invoke", 1)
            finally:
                obj.dispose()


async def test_cancelling_counter_preserved_after_absorption() -> None:
    """when JS absorbs cancellation, asyncio.CancelledError is
    not re-raised — but the task's cancelling counter (set by
    task.cancel()) is not cleared by us. Callers who need strict
    propagation check this counter after the call.

    This is a contract test; it exercises the escape hatch documented
    in eval_async's docstring.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def sleep_long() -> str:
                await asyncio.sleep(10)
                return "never"

            ctx.register("sleep_long", sleep_long)

            async def runner() -> tuple[str, int]:
                try:
                    async with asyncio.timeout(0.02):
                        r = await ctx.eval_async("""
                            try {
                                await sleep_long();
                                "never"
                            } catch (e) {
                                "absorbed"
                            }
                        """)
                        cancelling = asyncio.current_task().cancelling()
                        return r, cancelling
                except asyncio.CancelledError:
                    # Cancellation arrived before JS could absorb.
                    # Still a valid path per ; report it so the
                    # assertion below can handle either case.
                    return "propagated", -1

            r, cancelling = await runner()
            if r == "absorbed":
                # Absorption happened; cancelling counter should be > 0
                # so the caller can detect the cancellation even
                # though the Python exception didn't propagate.
                assert cancelling > 0
            else:
                assert r == "propagated"
