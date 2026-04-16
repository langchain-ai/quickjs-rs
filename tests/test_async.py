"""eval_async and eval_handle_async driving loop. §7.4, §11.1, §13.2."""

from __future__ import annotations

import asyncio

import pytest

from quickjs_wasm import (
    ConcurrentEvalError,
    DeadlockError,
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
    """§13.2 north star: auto-detected async host function, top-level
    await in module mode, single assertion."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def sleep_ms(n: int) -> str:
                await asyncio.sleep(n / 1000)
                return "slept"

            # §7.4 auto-detection: async def → is_async=True
            # without an explicit kwarg. Matches §13.2's @ctx.function form.
            ctx.register("sleep_ms", sleep_ms)
            assert await ctx.eval_async("await sleep_ms(10)") == "slept"


async def test_promise_all_fan_out() -> None:
    """Three concurrent async host calls via Promise.all. Exercises
    multiple in-flight tasks through one eval_async. §13.2."""
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
    """§7.4 concurrency rule: two eval_async against the same context
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
    """§10.3: pending top-level promise with nothing in flight → DeadlockError."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises(DeadlockError):
                await ctx.eval_async(
                    "new Promise((resolve) => {})", module=False
                )


async def test_mixed_sync_and_async_host_calls() -> None:
    """§13.2: one eval can call both sync and async host functions.
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
    HostError surfaces from eval_async via §10.2 routing."""
    from quickjs_wasm import HostError

    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def fail() -> None:
                await asyncio.sleep(0)
                raise ValueError("from async host")

            ctx.register("fail", fail)

            with pytest.raises(HostError) as excinfo:
                await ctx.eval_async("await fail()")
            assert "from async host" in excinfo.value.message
            assert isinstance(excinfo.value.__cause__, ValueError)


async def test_eval_async_per_call_timeout_override() -> None:
    """§7.4: timeout= kwarg on eval_async overrides the cumulative
    budget for that call. With the override set short, a slow async
    host call should abort with TimeoutError."""
    from quickjs_wasm import TimeoutError as _TimeoutError

    with Runtime() as rt:
        with rt.new_context(timeout=60.0) as ctx:

            async def very_slow() -> str:
                await asyncio.sleep(5.0)  # way longer than override
                return "done"

            ctx.register("very_slow", very_slow)

            with pytest.raises(_TimeoutError):
                await ctx.eval_async("await very_slow()", timeout=0.05)
