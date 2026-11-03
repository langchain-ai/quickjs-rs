"""ThreadWorker — pinning !Send Runtime/Context work to a single thread."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from quickjs_rs import Context, Runtime, ThreadWorker


def test_runtime_created_and_used_on_worker() -> None:
    """Runtime + Context created on the worker, eval'd from main thread
    without tripping the !Send panic."""
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


def test_close_without_explicit_dispose() -> None:
    """Leaving Runtime/Context undisposed: close() runs gc on the
    worker thread, so the !Send drop check succeeds. Without the gc
    pass on the worker, interpreter-shutdown gc would panic."""
    worker = ThreadWorker()

    async def _leak() -> None:
        rt = Runtime()
        rt.new_context()

    worker.run_sync(_leak())
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

        assert worker.run_sync(_eval()) == 4


async def test_async_context_manager() -> None:
    async with ThreadWorker() as worker:

        async def _make() -> tuple[Runtime, Context]:
            rt = Runtime()
            return rt, rt.new_context()

        rt, ctx = await worker.run_async(_make())

        async def _eval() -> int:
            return ctx.eval("10 - 3")

        assert await worker.run_async(_eval()) == 7


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


@pytest.mark.parametrize("direction", ["export", "import", "async_response", "arguments"])
async def test_cancellation_stops_marshalling_and_releases_worker(direction: str) -> None:
    """A cancelled caller must not leave a large conversion occupying the worker.

    The thread event proves that synchronous conversion actually unwound, rather
    than merely observing the caller's Future becoming cancelled. All operations
    use the public API and the context must remain reusable afterwards.
    """
    started = threading.Event()
    finished = threading.Event()
    payload = [{f"field_{j}": i + j for j in range(32)} for i in range(800)]
    with ThreadWorker() as worker:
        async def setup() -> tuple[Runtime, Context]:
            rt = Runtime()
            ctx = rt.new_context(timeout=30)
            if direction in {"export", "arguments"}:
                ctx.eval(
                    "globalThis.payload = Array.from({length: 800}, (_, i) => "
                    "Object.fromEntries(Array.from({length: 32}, (_, j) => "
                    "['field_' + j, i + j])))"
                )

            async def response() -> list[dict[str, int]]:
                started.set()
                return payload

            async def accept(value: Any) -> int:
                return len(value)

            ctx.register("response", response)
            ctx.register("accept", accept)
            return rt, ctx

        rt, ctx = await worker.run_async(setup())

        async def convert() -> None:
            try:
                if direction == "export":
                    with ctx.eval_handle("payload") as handle:
                        started.set()
                        handle.to_python()
                elif direction == "import":
                    started.set()
                    ctx.globals["imported"] = payload
                elif direction == "async_response":
                    await ctx.eval_async(
                        "try { await response(); } finally { globalThis.cleaned = true; }"
                    )
                else:
                    started.set()
                    await ctx.eval_async("await accept(payload)")
            finally:
                finished.set()

        submitted = worker.run_async(convert())
        assert await asyncio.to_thread(started.wait, 5)
        await asyncio.sleep(0.01)
        submitted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submitted
        assert await asyncio.to_thread(finished.wait, 1), "conversion still running after cancel"

        async def reuse_and_dispose() -> None:
            try:
                assert await ctx.eval_async("20 + 22") == 42
                if direction == "async_response":
                    assert ctx.eval("cleaned") is True
            finally:
                ctx.close()
                rt.close()

        await asyncio.wait_for(worker.run_async(reuse_and_dispose()), timeout=2)
