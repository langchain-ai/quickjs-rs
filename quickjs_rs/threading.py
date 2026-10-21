"""ThreadWorker — pin !Send Runtime/Context work to a single thread."""

from __future__ import annotations

import asyncio
import gc
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from types import TracebackType
from typing import Any


@dataclass
class _AsyncSubmission:
    """Own cancellation on the worker loop, independently of its result future."""

    coro: Coroutine[Any, Any, Any]
    task: asyncio.Task[Any] | None = None
    cancelling: bool = False

    async def run(self) -> Any:
        if self.cancelling:
            self.coro.close()
            raise asyncio.CancelledError
        self.task = asyncio.current_task()
        try:
            return await self.coro
        finally:
            self.task = None

    def cancel(self) -> None:
        self.cancelling = True
        if self.task is not None and not self.task.cancelling():
            self.task.cancel()


async def _drain_cancelled_submission(future: asyncio.Future[Any]) -> None:
    """Wait through repeated caller cancellation and retrieve the real outcome."""
    completion = asyncio.gather(future, return_exceptions=True)
    while True:
        try:
            result = await asyncio.shield(completion)
            break
        except asyncio.CancelledError:
            continue
    if isinstance(result[0], Exception):
        raise asyncio.CancelledError from result[0]


def run_coroutine_on_loop(
    coro: Coroutine[Any, Any, Any], *, loop: asyncio.AbstractEventLoop
) -> asyncio.Future[Any]:
    """Submit a coroutine to a loop and await its actual cancellation cleanup.

    Unlike cancelling a ``run_coroutine_threadsafe`` result future, cancelling
    the returned future waits for the task on ``loop`` to finish its cleanup.
    Repeated cancellation does not interrupt that cleanup. The caller's current
    context variables are preserved.

    Args:
        coro: Coroutine owned by the target loop once submitted.
        loop: Running target event loop, possibly on another thread.

    Returns:
        A future on the caller's running event loop.
    """
    submitted = False

    async def wait() -> Any:
        nonlocal submitted
        submitted = True
        operation = _AsyncSubmission(coro)
        future = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(operation.run(), loop))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            loop.call_soon_threadsafe(operation.cancel)
            await _drain_cancelled_submission(future)
            raise

    pending = asyncio.create_task(wait())

    def close_unsubmitted(_task: asyncio.Task[Any]) -> None:
        # A task cancelled before its first step never enters wait's body.
        if not submitted:
            coro.close()

    pending.add_done_callback(close_unsubmitted)
    return pending


class ThreadWorker:
    """Dedicated OS thread with its own asyncio event loop.

    ``Runtime`` and ``Context`` are ``!Send`` at the Rust level: they carry
    ``Rc<RefCell<...>>`` internals and QuickJS itself is single-threaded, so
    using them from any thread other than the creator panics with an
    "unsendable" assertion.

    ``ThreadWorker`` pins all Runtime/Context work to a single thread.
    Callers on any thread submit coroutines via ``run_sync`` (blocks the
    caller) or ``run_async`` (returns an awaitable tied to the caller's
    loop). The worker starts lazily on first submission and runs as a
    daemon thread.

    ``close()`` forces ``gc.collect()`` on the worker thread so ``!Send``
    QuickJS objects are finalized on their owning thread, then stops the
    loop and joins. Without the gc pass, a later collection on any other
    thread (e.g. an interpreter-shutdown sweep) would hit the ``!Send``
    drop check and panic.
    """

    def __init__(self, name: str = "quickjs-worker") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()

    def _ensure_started(self) -> None:
        if self._loop is not None:
            return
        with self._start_lock:
            if self._loop is not None:
                return

            def _runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                        for t in pending:
                            t.cancel()
                        if pending:
                            loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True)
                            )
                    except Exception:
                        # Shutdown is best-effort.
                        pass
                    loop.close()

            self._thread = threading.Thread(target=_runner, name=self._name, daemon=True)
            self._thread.start()
            self._ready.wait()

    def run_sync(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Submit ``coro`` to the worker loop and block until it completes."""
        self._ensure_started()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def run_async(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Future[Any]:
        """Submit work and acknowledge cancellation after worker cleanup completes."""
        self._ensure_started()
        assert self._loop is not None
        return run_coroutine_on_loop(coro, loop=self._loop)

    def close(self) -> None:
        """Stop the worker thread and join. Idempotent."""
        if self._loop is None or self._thread is None:
            return

        async def _gc() -> None:
            gc.collect()

        try:
            asyncio.run_coroutine_threadsafe(_gc(), self._loop).result(timeout=1.0)
        except Exception:
            # Best-effort; don't block shutdown.
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop = None
        self._thread = None

    def __enter__(self) -> ThreadWorker:
        self._ensure_started()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> ThreadWorker:
        self._ensure_started()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
