"""ThreadWorker — pin !Send Runtime/Context work to a single thread."""

from __future__ import annotations

import asyncio
import gc
import threading
from collections.abc import Coroutine
from types import TracebackType
from typing import Any


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
