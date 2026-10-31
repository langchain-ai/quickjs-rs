"""ThreadWorker — run Runtime/Context work on one dedicated thread."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import threading
from collections.abc import Coroutine
from types import TracebackType
from typing import Any


class ThreadWorker:
    """Dedicated OS thread with its own asyncio event loop.

    QuickJS is single-threaded: a ``Context`` must only be driven by one
    thread at a time, and its async host calls are scheduled on the loop that
    started the eval. ``ThreadWorker`` gives a set of ``Runtime``/``Context``
    objects one home thread and loop, so JS work runs off the caller's loop
    and calls from many threads are serialized.

    Callers on any thread submit coroutines via ``run_sync`` (blocks the
    caller) or ``run_async`` (returns an awaitable tied to the caller's
    loop). The worker starts lazily on first submission and runs as a
    daemon thread.

    The worker tears itself down: stopping its loop makes the worker thread
    cancel leftover tasks, run ``gc.collect()`` so QuickJS objects are
    finalized there, and close its loop. ``aclose()`` awaits that without
    blocking the caller's loop; ``close()`` joins the thread.
    """

    def __init__(self, name: str = "quickjs-worker") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        # Guards `_exited` and `_exit_waiters`, so a waiter registered by
        # `aclose()` is either resolved by the worker or sees it has exited.
        self._exit_lock = threading.RLock()
        self._exited = False
        self._exit_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    def _ensure_started(self) -> None:
        if self._loop is not None:
            return
        with self._start_lock:
            if self._loop is not None:
                return
            # Fresh state in case this worker was closed and is restarting.
            self._ready.clear()
            with self._exit_lock:
                self._exited = False

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
                    # Finalize QuickJS objects on the thread that used them.
                    gc.collect()
                    loop.close()
                    self._signal_exited()

            self._thread = threading.Thread(
                target=_runner, name=self._name, daemon=True
            )
            self._thread.start()
            self._ready.wait()

    def _signal_exited(self) -> None:
        with self._exit_lock:
            self._exited = True
            waiters, self._exit_waiters = self._exit_waiters, []
        for caller_loop, fut in waiters:
            # The caller's loop may already be closed; nothing to wake then.
            with contextlib.suppress(RuntimeError):
                caller_loop.call_soon_threadsafe(_resolve, fut)

    def run_sync(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Submit ``coro`` to the worker loop and block until it completes."""
        self._ensure_started()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def run_async(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Future[Any]:
        """Submit ``coro`` to the worker loop; return a future on the caller's loop."""
        self._ensure_started()
        assert self._loop is not None
        return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self._loop))

    def _detach(self) -> tuple[asyncio.AbstractEventLoop, threading.Thread] | None:
        with self._exit_lock:
            loop, thread = self._loop, self._thread
            if loop is None or thread is None:
                return None
            self._loop = None
            self._thread = None
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)
        return loop, thread

    def close(self) -> None:
        """Stop the worker thread and join. Idempotent.

        Blocks the calling thread for up to 5 s. From a coroutine, use
        ``aclose()``.
        """
        detached = self._detach()
        if detached is not None:
            detached[1].join(timeout=5.0)

    async def aclose(self, timeout: float | None = 5.0) -> None:
        """Stop the worker thread without blocking the caller's loop. Idempotent.

        Waits up to ``timeout`` seconds for the worker to finish its own
        teardown. If it doesn't (a host call stuck in blocking code), the
        daemon thread is abandoned and exits whenever it unblocks.
        """
        caller_loop = asyncio.get_running_loop()
        with self._exit_lock:
            if self._loop is None or self._thread is None:
                return
            done: asyncio.Future[None] = caller_loop.create_future()
            if self._loop is not caller_loop and not self._exited:
                self._exit_waiters.append((caller_loop, done))
            else:
                # Already exited, or called from the worker's own loop, which
                # can't wait for itself to stop.
                done.set_result(None)
            self._detach()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(done, timeout)

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
        await self.aclose()


def _resolve(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)
