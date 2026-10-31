"""ThreadWorker — run Runtime/Context work on one dedicated thread."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from types import TracebackType
from typing import Any


class ThreadWorker:
    """Dedicated OS thread with its own asyncio event loop.

    A ``Context`` is single-threaded: only one thread may drive it at a
    time. ``ThreadWorker`` runs all Runtime/Context work on one thread, off
    the caller's event loop. Callers on any thread submit coroutines via
    ``run_sync`` (blocks the caller) or ``run_async`` (returns an awaitable
    tied to the caller's loop). The worker starts lazily on first submission
    and runs as a daemon thread.

    Close your ``Context`` and ``Runtime`` (e.g. via ``run_async``) before
    closing the worker. Stopping the worker cancels any tasks still on its
    loop and closes the loop.
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

            self._thread = threading.Thread(
                target=_runner, name=self._name, daemon=True
            )
            self._thread.start()
            self._ready.wait()

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

    def close(self) -> None:
        """Stop the worker thread and join. Idempotent."""
        thread = self._stop()
        if thread is not None:
            thread.join(timeout=5.0)

    async def aclose(self) -> None:
        """Stop the worker without blocking the caller's loop. Idempotent.

        The daemon thread cancels its leftover tasks, closes its loop, and
        exits on its own.
        """
        self._stop()

    def _stop(self) -> threading.Thread | None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return None
        self._loop = None
        self._thread = None
        loop.call_soon_threadsafe(loop.stop)
        return thread

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
