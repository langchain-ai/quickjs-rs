"""eval_async and eval_handle_async driving loop."""

from __future__ import annotations

import asyncio

import pytest

from quickjs_rs import (
    ConcurrentEvalError,
    DeadlockError,
    JSError,
    Runtime,
)


async def test_eval_async_fast_path_non_promise() -> None:
    """Pure sync code: eval_async returns without entering the driving
    loop. Fast path exists because most eval_async calls in practice
    — especially inside agent loops — don't actually await anything
    JS-side; they just mix sync and async host calls."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            assert await ctx.eval_async("1 + 2", module=False) == 3


async def test_eval_async_first_assertion_from_132() -> None:
    """Auto-detected async host function, top-level
    await in module mode, single assertion."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def sleep_ms(n: int) -> str:
                await asyncio.sleep(n / 1000)
                return "slept"

            # auto-detection: async def → is_async=True
            # without an explicit kwarg. Matches 's @ctx.function form.
            ctx.register("sleep_ms", sleep_ms)
            assert await ctx.eval_async("await sleep_ms(10)") == "slept"


async def test_promise_all_fan_out() -> None:
    """Three concurrent async host calls via Promise.all. Exercises
    multiple in-flight tasks through one eval_async. ."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def sleep_ms(n: int) -> str:
                await asyncio.sleep(n / 1000)
                return "slept"

            ctx.register("sleep_ms", sleep_ms)
            result = await ctx.eval_async("""
                const results = await Promise.all([
                    sleep_ms(5),
                    sleep_ms(10),
                    sleep_ms(15),
                ]);
                results.join(",")
            """)
            assert result == "slept,slept,slept"


async def test_concurrent_eval_async_raises() -> None:
    """Concurrency rule: two eval_async against the same context
    simultaneously raises ConcurrentEvalError on the second one."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def slow() -> str:
                await asyncio.sleep(0.05)
                return "ok"

            ctx.register("slow", slow)

            async def first() -> None:
                await ctx.eval_async("await slow()")

            async with asyncio.TaskGroup() as tg:
                tg.create_task(first())
                # Let first() enter the driving loop before the
                # second one tries to start.
                await asyncio.sleep(0.001)
                with pytest.raises(ConcurrentEvalError):
                    await ctx.eval_async("1 + 1")


async def test_deadlock_error_when_no_resolver() -> None:
    """Pending top-level promise with nothing in flight →
    DeadlockError.

    Returning a pending promise as the last expression would be
    wrapped as ``{value: <pending>, done: false}`` — the wrapper
    itself is fulfilled, so the driving loop returns immediately
    without hitting the deadlock path.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises(DeadlockError):
                await ctx.eval_async("await new Promise((resolve) => {})", module=False)


async def test_mixed_sync_and_async_host_calls() -> None:
    """One eval can call both sync and async host functions.
    Sync returns immediately, async awaits — the driving loop handles
    both paths in one evaluation."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            def double(n: int) -> int:
                return n * 2

            async def slow_double(n: int) -> int:
                await asyncio.sleep(0.001)
                return n * 2

            ctx.register("double", double)
            ctx.register("slow_double", slow_double)

            result = await ctx.eval_async("""
                const a = double(5);
                const b = await slow_double(10);
                a + b
            """)
            assert result == 30


async def test_eval_handle_async_returns_handle() -> None:
    """eval_handle_async mirrors eval_async but keeps the settled value
    as a Handle rather than marshaling out. Useful when the result is
    a JS object with methods the caller wants to keep invoking."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def settle() -> dict[str, int]:
                await asyncio.sleep(0)
                return {"value": 7}

            ctx.register("settle", settle)

            h = await ctx.eval_handle_async("await settle()")
            try:
                assert h.type_of == "object"
                value_handle = h.get("value")
                try:
                    assert value_handle.to_python() == 7
                finally:
                    value_handle.dispose()
            finally:
                h.dispose()


async def test_eval_async_propagates_js_rejection() -> None:
    """An async host fn that raises → rejected Promise →
    original exception bubbles out of eval_async uncaught."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def fail() -> None:
                await asyncio.sleep(0)
                raise ValueError("from async host")

            ctx.register("fail", fail)

            with pytest.raises(ValueError, match="from async host"):
                await ctx.eval_async("await fail()")


async def test_handle_await_promise_happy_path() -> None:
    """Tail: eval_handle_async + await_promise chain. Get a
    Handle to a Promise.resolve(42), await it, verify the resolved
    value round-trips."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            p = await ctx.eval_handle_async("Promise.resolve(42)")
            try:
                resolved = await p.await_promise()
                try:
                    assert resolved.to_python() == 42
                finally:
                    resolved.dispose()
            finally:
                p.dispose()


async def test_handle_await_promise_rejection_raises_jserror() -> None:
    """A Promise that rejects with a JS Error surfaces as JSError
    on await_promise. Routing applies exactly as it does for
    eval_async on the same promise."""
    from quickjs_rs import JSError

    with Runtime() as rt:
        with rt.new_context() as ctx:
            # eval_handle_async settles the module promise (with
            # the iterator-result envelope) but the inner value is
            # the rejected inner promise — we need the caller to get
            # a handle to a rejected Promise directly. Use a script-
            # mode eval_handle to skip the async envelope, then hand
            # the raw Promise to await_promise.
            p = ctx.eval_handle("Promise.reject(new TypeError('boom'))")
            try:
                with pytest.raises(JSError) as excinfo:
                    await p.await_promise()
                assert excinfo.value.name == "TypeError"
                assert excinfo.value.message == "boom"
            finally:
                p.dispose()


async def test_handle_await_promise_shares_driving_machinery() -> None:
    """await_promise traffics through _run_inside_task_group just like
    eval_async does. The cancellation / absorption / deadline wiring
    is shared, so the per-path cancellation tests in
    test_async_host_functions.py exercise it end-to-end through both
    entry points transitively.

    Constructing a "handle to a promise with pending in-flight host
    work" to test await_promise cancellation directly turns out to
    require either bypassing step 9's sync-eval guard (not allowed)
    or racing against TaskGroup teardown semantics (eval_handle_async
    waits for its child tasks on happy exit, so the resulting handle
    never has live in-flight host work). Rather than engineering
    around either constraint, this test confirms await_promise
    dispatches correctly on a simple Promise.resolve, and the
    cancellation contract is inherited from the shared machinery.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            p = await ctx.eval_handle_async("Promise.resolve('done')")
            try:
                resolved = await p.await_promise()
                try:
                    assert resolved.to_python() == "done"
                finally:
                    resolved.dispose()
            finally:
                p.dispose()


async def test_handle_await_promise_non_promise_returns_self() -> None:
    """Idiomatic fast path: calling await_promise on a Handle
    that isn't a Promise returns self unchanged. Lets callers write
    `await h.await_promise()` without pre-checking is_promise when
    the JS-side callee may return either shape."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # A non-promise value.
            h = ctx.eval_handle("({x: 1})")
            try:
                returned = await h.await_promise()
                # Identity: the same handle is returned, not a new one.
                assert returned is h
                # Still usable after await_promise.
                x = returned.get("x")
                try:
                    assert x.to_python() == 1
                finally:
                    x.dispose()
            finally:
                h.dispose()


async def test_handle_await_promise_concurrent_eval_raises() -> None:
    """await_promise respects the concurrent-eval guard: if an
    eval_async is already in flight on the same context, await_promise
    raises ConcurrentEvalError. Same rule applies in reverse
    (eval_async while await_promise is running) — same guard, same
    boolean flag."""
    from quickjs_rs import ConcurrentEvalError

    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def sleep_ms(n: int) -> str:
                await asyncio.sleep(n / 1000)
                return "slept"

            ctx.register("sleep_ms", sleep_ms)

            async def first() -> None:
                await ctx.eval_async("await sleep_ms(50)")

            # Create a handle to a standalone Promise we'll try to
            # await concurrently with the first eval_async.
            p = ctx.eval_handle("Promise.resolve(1)")
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(first())
                    await asyncio.sleep(0.005)  # let first() enter
                    with pytest.raises(ConcurrentEvalError):
                        await p.await_promise()
            finally:
                p.dispose()


async def test_per_call_timeout_excludes_host_call_time() -> None:
    """Host-function await time is excluded from eval_async timeout
    accounting."""
    with Runtime() as rt:
        with rt.new_context(timeout=60.0) as ctx:

            async def slow() -> str:
                # Comfortably longer than the per-call timeout below.
                await asyncio.sleep(0.2)
                return "done"

            ctx.register("slow", slow)

            assert await ctx.eval_async("await slow()", timeout=0.05) == "done"


async def test_promise_chain_resolves_synchronously() -> None:
    """A JS-side Promise chain (Promise.resolve(x).then(...)) settles
    via microtasks — no async host calls in flight, no external
    signal needed. The driving loop's drain-then-check order plus
    run_pending_jobs is what makes this work: the .then handler runs
    as a microtask during the drain, and step 3 (fulfilled) reads
    the post-drain state.

    Explicit test because the sync-resolution path has a different
    shape from the host-call-driven path (no _pending_completed
    event ever fires) and they should both just work."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            result = await ctx.eval_async("await Promise.resolve(1).then(x => x + 1)")
            assert result == 2


async def test_throw_in_then_callback_propagates_as_js_error() -> None:
    """A throw inside a .then() reaction fires as a pending job.
    run_pending_jobs must propagate the actual JS exception — name,
    message, stack — not swallow it or raise a generic string."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises(JSError) as exc_info:
                await ctx.eval_async(
                    "await Promise.resolve()"
                    ".then(() => { throw new RangeError('boom'); })"
                )
            assert exc_info.value.name == "RangeError"
            assert "boom" in exc_info.value.message


async def test_cumulative_budget_excludes_host_call_time() -> None:
    """Two eval_async calls use independent per-eval timeout budgets."""
    with Runtime() as rt:
        # Combined wall time exceeds one timeout window, but each call
        # gets a fresh budget.
        with rt.new_context(timeout=0.05) as ctx:

            async def tiny() -> str:
                await asyncio.sleep(0.03)
                return "ok"

            ctx.register("tiny", tiny)

            assert await ctx.eval_async("await tiny()") == "ok"
            assert await ctx.eval_async("await tiny()") == "ok"


async def test_cumulative_budget_still_trips_on_js_time() -> None:
    """Host time is reclaimed, but pure JS execution still counts.
    A tight JS loop dispatched via eval_async with no host calls
    must trip the cumulative budget via the interrupt handler."""
    from quickjs_rs import TimeoutError as _TimeoutError

    with Runtime() as rt:
        with rt.new_context(timeout=0.1) as ctx:
            with pytest.raises(_TimeoutError):
                await ctx.eval_async("while (true) {}", module=False)


async def test_cumulative_budget_drained_by_wall_time_outside_eval() -> None:
    """Wall-clock outside eval_async should not consume later
    timeout budget."""

    with Runtime() as rt:
        with rt.new_context(timeout=0.05) as ctx:
            # Idle well past timeout before starting eval.
            await asyncio.sleep(0.1)
            assert await ctx.eval_async("1 + 1", module=False) == 2


async def test_per_call_timeout_override_lifts_above_cumulative() -> None:
    """timeout= kwarg on eval_async overrides the default timeout for
    that call only."""
    from quickjs_rs import TimeoutError as _TimeoutError

    with Runtime() as rt:
        with rt.new_context(timeout=0.01) as ctx:
            # Default timeout is too short for pure JS runaway compute.
            with pytest.raises(_TimeoutError):
                await ctx.eval_async("while(true){}", module=False)

            # Override extends this call only.
            assert await ctx.eval_async("1 + 1", module=False, timeout=0.2) == 2


def test_sync_eval_does_not_decrement_cumulative_budget() -> None:
    """Sync eval and eval_async each use per-call timeout budgets."""
    import time as _time

    async def body() -> None:
        with Runtime() as rt:
            with rt.new_context(timeout=1.0) as ctx:
                start = _time.monotonic()
                # Several sync evals.
                for _ in range(3):
                    assert ctx.eval("1 + 1", module=False) == 2
                # Sanity: sync evals stayed well within the sync
                # per-call budget (1s). If they individually took a
                # long time, this test wouldn't prove what it's
                # trying to prove.
                assert _time.monotonic() - start < 0.5

                # Prior sync evals should not affect this call's
                # timeout budget.
                async def sleepy() -> str:
                    await asyncio.sleep(0.05)
                    return "ok"

                ctx.register("sleepy", sleepy)
                assert await ctx.eval_async("await sleepy()") == "ok"
                # If sync eval consumed async timeout budget, this
                # could have raised TimeoutError.

    asyncio.run(body())
