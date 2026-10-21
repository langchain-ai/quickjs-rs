"""Cancellation acknowledges worker cleanup, including repeated caller cancellation."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
from collections.abc import Callable

import pytest

from quickjs_rs import ThreadWorker


@pytest.mark.parametrize("repeat", [False, True])
async def test_cancel_waits_for_real_worker_finally(repeat: bool) -> None:
    worker = ThreadWorker()
    entered, cleaning, finished = (threading.Event() for _ in range(3))
    release: list[Callable[[], None]] = []

    async def operation() -> None:
        loop = asyncio.get_running_loop()
        gate = loop.create_future()

        def release_gate() -> None:
            if not gate.done():
                gate.set_result(None)

        release.append(lambda: loop.call_soon_threadsafe(release_gate))
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await gate
            finished.set()

    pending = worker.run_async(operation())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        assert await asyncio.to_thread(cleaning.wait, 3)
        assert not pending.done(), (
            "Caller cancellation returned before the worker's finally finished"
        )
        if repeat:
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
        release[0]()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(3):
                await pending
        assert finished.is_set()
        assert await worker.run_async(asyncio.sleep(0, result=42)) == 42
    finally:
        if release and not finished.is_set():
            release[0]()
        await asyncio.to_thread(finished.wait, 3)
        await asyncio.gather(pending, return_exceptions=True)
        worker.close()


async def test_immediate_cancellation_closes_unstarted_coroutine() -> None:
    worker = ThreadWorker()
    called = False

    async def operation() -> None:
        nonlocal called
        called = True

    coro = operation()
    pending = worker.run_async(coro)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await asyncio.sleep(0)
    assert not called
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    worker.close()


async def test_async_submission_preserves_contextvars() -> None:
    identity = contextvars.ContextVar("identity", default="missing")
    worker = ThreadWorker()

    async def operation() -> str:
        return identity.get()

    token = identity.set("authorized-owner")
    try:
        pending = worker.run_async(operation())
    finally:
        identity.reset(token)
    try:
        assert await pending == "authorized-owner"
    finally:
        worker.close()


async def test_cleanup_failure_remains_visible_on_cancellation() -> None:
    worker = ThreadWorker()
    entered = threading.Event()

    async def operation() -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            raise RuntimeError("cleanup failed")

    pending = worker.run_async(operation())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await pending
        assert isinstance(cancelled.value.__cause__, RuntimeError)
        assert str(cancelled.value.__cause__) == "cleanup failed"
    finally:
        await asyncio.gather(pending, return_exceptions=True)
        worker.close()
