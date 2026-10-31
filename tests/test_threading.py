"""ThreadWorker — running Runtime/Context work on one dedicated thread."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from quickjs_rs import Context, Runtime, ThreadWorker


def test_runtime_created_and_used_on_worker() -> None:
    """Runtime + Context created on the worker, driven from the main thread."""
    worker = ThreadWorker()

    async def _make() -> tuple[Runtime, Context]:
        rt = Runtime()
        return rt, rt.new_context()

    async def _eval(ctx: Context) -> int:
        return ctx.eval("1 + 2")

    async def _dispose(rt: Runtime, ctx: Context) -> None:
        ctx.close()
        rt.close()

    rt, ctx = worker.run_sync(_make())
    assert worker.run_sync(_eval(ctx)) == 3
    worker.run_sync(_dispose(rt, ctx))
    worker.close()


def test_many_caller_threads_share_one_worker() -> None:
    """Runtime/Context live on the worker; many caller threads submit
    concurrently. All succeed — none panic because the QuickJS work
    stays on a single thread."""
    worker = ThreadWorker()

    async def _make() -> tuple[Runtime, Context]:
        rt = Runtime()
        return rt, rt.new_context()

    rt, ctx = worker.run_sync(_make())

    async def _eval(expr: str) -> int:
        return ctx.eval(expr)

    def _caller(i: int) -> int:
        return worker.run_sync(_eval(f"{i} * 2"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_caller, range(16)))

    assert results == [i * 2 for i in range(16)]

    async def _dispose() -> None:
        ctx.close()
        rt.close()

    worker.run_sync(_dispose())
    worker.close()


async def test_run_async_from_async_caller() -> None:
    """run_async returns a future on the caller's loop; a caller can
    await worker-bound work without blocking their own loop."""
    worker = ThreadWorker()

    async def _make() -> tuple[Runtime, Context]:
        rt = Runtime()
        return rt, rt.new_context()

    rt, ctx = worker.run_sync(_make())

    async def _eval() -> int:
        return ctx.eval("7 * 6")

    assert await worker.run_async(_eval()) == 42

    async def _dispose() -> None:
        ctx.close()
        rt.close()

    await worker.run_async(_dispose())
    worker.close()


def test_sync_caller_with_async_host_function() -> None:
    """A sync caller can drive eval_async through the worker. The
    worker's loop handles the Promise-resolution machinery so host
    functions can be ``async def``."""
    worker = ThreadWorker()

    async def _setup() -> tuple[Runtime, Context]:
        rt = Runtime()
        ctx = rt.new_context()

        async def sleep_ms(n: int) -> str:
            await asyncio.sleep(n / 1000)
            return "slept"

        ctx.register("sleep_ms", sleep_ms)
        return rt, ctx

    rt, ctx = worker.run_sync(_setup())

    async def _run() -> object:
        return await ctx.eval_async("await sleep_ms(5)")

    assert worker.run_sync(_run()) == "slept"

    async def _dispose() -> None:
        ctx.close()
        rt.close()

    worker.run_sync(_dispose())
    worker.close()


def test_close_is_idempotent() -> None:
    worker = ThreadWorker()
    worker.run_sync(asyncio.sleep(0))
    worker.close()
    worker.close()


def test_lazy_start() -> None:
    """No thread is spawned until the first submission."""
    worker = ThreadWorker()
    assert worker._thread is None
    worker.run_sync(asyncio.sleep(0))
    assert worker._thread is not None
    assert worker._thread.is_alive()
    worker.close()


def test_worker_thread_name() -> None:
    worker = ThreadWorker(name="custom-worker")

    async def _probe() -> str:
        return threading.current_thread().name

    assert worker.run_sync(_probe()) == "custom-worker"
    worker.close()


def test_sync_context_manager() -> None:
    with ThreadWorker() as worker:

        async def _make() -> tuple[Runtime, Context]:
            rt = Runtime()
            return rt, rt.new_context()

        rt, ctx = worker.run_sync(_make())

        async def _eval() -> int:
            return ctx.eval("2 + 2")

        async def _dispose() -> None:
            ctx.close()
            rt.close()

        assert worker.run_sync(_eval()) == 4
        worker.run_sync(_dispose())


async def test_async_context_manager() -> None:
    async with ThreadWorker() as worker:

        async def _make() -> tuple[Runtime, Context]:
            rt = Runtime()
            return rt, rt.new_context()

        rt, ctx = await worker.run_async(_make())

        async def _eval() -> int:
            return ctx.eval("10 - 3")

        async def _dispose() -> None:
            ctx.close()
            rt.close()

        assert await worker.run_async(_eval()) == 7
        await worker.run_async(_dispose())


def test_exception_propagates_to_sync_caller() -> None:
    worker = ThreadWorker()

    async def _boom() -> None:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        worker.run_sync(_boom())

    worker.close()


async def test_exception_propagates_to_async_caller() -> None:
    worker = ThreadWorker()

    async def _boom() -> None:
        raise RuntimeError("kaboom-async")

    with pytest.raises(RuntimeError, match="kaboom-async"):
        await worker.run_async(_boom())

    worker.close()


async def test_aclose_does_not_block_caller_loop() -> None:
    """aclose() only posts the stop; teardown runs on the worker thread."""
    worker = ThreadWorker()

    async def _make() -> tuple[Runtime, Context]:
        rt = Runtime()
        return rt, rt.new_context()

    async def _dispose(rt: Runtime, ctx: Context) -> None:
        ctx.close()
        rt.close()

    async def _slow_teardown() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            time.sleep(0.3)  # blocks the worker thread, not the caller
            raise

    rt, ctx = await worker.run_async(_make())
    await worker.run_async(_dispose(rt, ctx))
    assert ctx._engine_ctx._inst._closed  # the wasm Store is closed
    worker.run_async(_slow_teardown())
    await asyncio.sleep(0.02)
    thread = worker._thread
    assert thread is not None

    gaps: list[float] = []

    async def _beat() -> None:
        last = time.monotonic()
        while thread.is_alive():
            await asyncio.sleep(0.005)
            gaps.append(time.monotonic() - last)
            last = time.monotonic()

    beat = asyncio.create_task(_beat())
    await asyncio.sleep(0.02)
    await worker.aclose()
    await asyncio.wait_for(beat, timeout=2.0)
    assert max(gaps) < 0.1


async def test_aclose_is_idempotent() -> None:
    worker = ThreadWorker()
    await worker.aclose()  # never started
    await worker.run_async(asyncio.sleep(0))
    await worker.aclose()
    await worker.aclose()
    worker.close()
