"""Async host-function registration. See spec/implementation.md §7.4, §11.1."""

from __future__ import annotations

import asyncio
import functools

import pytest

from quickjs_wasm import Runtime


async def test_async_def_auto_detected_via_decorator() -> None:
    """§7.4: @ctx.function on an async def auto-detects as async."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            async def tick() -> str:
                await asyncio.sleep(0)
                return "tock"

            assert await ctx.eval_async("await tick()") == "tock"


async def test_async_def_auto_detected_via_register() -> None:
    """§7.4: ctx.register(name, fn) with no is_async kwarg auto-detects."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def lookup(k: str) -> int:
                await asyncio.sleep(0)
                return len(k)

            ctx.register("lookup", lookup)
            assert await ctx.eval_async("await lookup('hello')") == 5


async def test_sync_def_auto_detected_as_sync() -> None:
    """Regression: plain def functions still register as sync under the
    new auto-detection default. Guards against §7.4 default
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
    """§7.4: a non-coroutine wrapper around a coroutine (the broken-
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


# ---- §7.4 cancellation (step 7) ---------------------------------------


async def test_cancel_propagates_through_eval_async() -> None:
    """§13.2: task.cancel() on an in-flight eval_async re-raises
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
    """§7.4: JS catching HostCancellationError and returning normally
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
                    # Acceptable alternate path per §13.2: cancellation
                    # propagated before the JS catch handler ran. On
                    # slower systems the timeout might fire before JS
                    # even reaches its catch block.
                    return "propagated"

            result = await runner()
            # Either JS absorbed (returning the error name) or the
            # cancellation propagated before JS saw it. Both are
            # spec-valid per §13.2.
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
                task = asyncio.create_task(
                    ctx.eval_async("while(true){}", module=False)
                )
                await asyncio.sleep(0.05)
                task.cancel()
                await task


async def test_cancel_finally_host_calls_also_cancelled() -> None:
    """§7.4 subtle case: JS `finally` that awaits another host call
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

            task = asyncio.create_task(ctx.eval_async("""
                try {
                    await sleep_long();
                } finally {
                    await cleanup();
                }
            """))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # cleanup() was dispatched and started, but the TaskGroup
            # cancellation prevented its sleep from completing —
            # cleanup_completed stays False. The cleanup code RAN but
            # didn't finish its await. This is the spec-locked
            # semantic.
            assert cleanup_completed is False


async def test_sync_eval_with_async_hostfn_raises_concurrent_eval_error() -> None:
    """§13.2 / §7.4: sync ctx.eval that invokes a registered async host
    function raises ConcurrentEvalError. The detection happens at the
    dispatcher level (no asyncio loop → set flag) and surfaces when
    eval returns."""
    from quickjs_wasm import ConcurrentEvalError

    def runner() -> None:
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

    # Run the body in a dedicated thread so there's genuinely no
    # asyncio loop in sight during ctx.eval. (pytest-asyncio's auto
    # mode puts us inside a running loop by default; we need to be
    # outside it to exercise the "no running loop" dispatcher path.)
    import threading

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "sync-eval test thread hung"


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
    'eval returned a Promise, raise', that's a regression against a
    legitimate usage pattern.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # eval returns a Promise handle; the eval ITSELF has no
            # async host calls, just a pure-JS Promise. Should be
            # a MarshalError (Promises aren't marshalable to Python)
            # — NOT a ConcurrentEvalError.
            from quickjs_wasm import MarshalError

            with pytest.raises(MarshalError):
                ctx.eval("Promise.resolve(42)")

            # eval_handle path: returns a Handle to the Promise, no
            # error. User can drive it via await_promise later.
            h = ctx.eval_handle("Promise.resolve(42)")
            try:
                assert h.is_promise
            finally:
                h.dispose()


def test_sync_eval_js_try_catch_still_raises_concurrent_eval_error() -> None:
    """Edge case: JS code that wraps the async host call in a
    try/catch still surfaces ConcurrentEvalError to Python. JS
    'handling' the rejection doesn't mean the Python caller got
    what they asked for — surfacing the error tells them to use
    eval_async. The flag-based detection does this naturally
    because the flag is set at dispatcher time, before JS's catch
    handler runs."""
    from quickjs_wasm import ConcurrentEvalError

    def runner() -> None:
        with Runtime() as rt:
            with rt.new_context() as ctx:

                async def slow() -> int:
                    return 1

                ctx.register("slow", slow)

                with pytest.raises(ConcurrentEvalError):
                    ctx.eval(
                        "try { slow(); 'unreachable' } "
                        "catch (e) { e.name }"
                    )

    import threading

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive()


async def test_cancelling_counter_preserved_after_absorption() -> None:
    """§7.4: when JS absorbs cancellation, asyncio.CancelledError is
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
                    # Still a valid path per §13.2; report it so the
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
