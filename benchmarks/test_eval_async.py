"""Async eval benchmarks. See benchmarks/README.md.

Measure overhead of the async driving loop, TaskGroup, and event-
loop interaction — not the host function's own latency. The
``async_ctx`` fixture from conftest pre-registers an ``instant``
async host fn that returns immediately, so these benchmarks
isolate pipeline cost.

All tests use ``@pytest.mark.benchmark`` (whole-test timing)
because the benchmark body is async. pytest-asyncio's auto mode
drives them.
"""

from __future__ import annotations

import pytest

from quickjs_rs import Context


@pytest.mark.benchmark
async def bench_eval_async_noop(async_ctx: Context) -> None:
    """await ctx.eval_async('undefined') — async pipeline minimum.
    Floor for the async eval cost with no host calls and no
    top-level await inside JS."""
    await async_ctx.eval_async("undefined")


@pytest.mark.benchmark
async def bench_eval_async_immediate_host(async_ctx: Context) -> None:
    """Async host fn that returns immediately (no sleep) — measures
    pure dispatch + promise-settle overhead without any genuine
    wait. The ``instant`` host fn is pre-registered by the
    async_ctx fixture."""
    await async_ctx.eval_async("await instant(42)")


@pytest.mark.benchmark
async def bench_eval_async_fan_out_10(async_ctx: Context) -> None:
    """Promise.all with 10 immediate async host calls — concurrent
    dispatch overhead. Exercises the TaskGroup's ability to host
    multiple in-flight tasks and the driving loop's event-wait
    pattern under fan-out."""
    code = """
        await Promise.all([
            instant(1), instant(2), instant(3), instant(4), instant(5),
            instant(6), instant(7), instant(8), instant(9), instant(10),
        ])
    """
    await async_ctx.eval_async(code)


@pytest.mark.benchmark
async def bench_eval_async_sequential_10(async_ctx: Context) -> None:
    """Ten sequential await calls to immediate async host fns —
    driving loop iteration cost. Each await settles before the
    next dispatches, so the loop iterates 10 times through
    drain-check-wait.

    Wrapped in an IIFE for the same reason as the sync-eval
    benchmarks: top-level `let` redeclares across pytest-codspeed
    iterations on the same Context.
    """
    code = """
        await (async () => {
            let x = 0;
            for (let i = 0; i < 10; i++) {
                x = await instant(x + 1);
            }
            return x;
        })()
    """
    await async_ctx.eval_async(code)
