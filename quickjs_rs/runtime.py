"""Runtime. See spec/implementation.md §7."""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import QuickJSError


class Runtime:
    """Owns the QuickJS runtime (JS heap, memory/stack limits, interrupt
    handler). Thin wrapper over ``_engine.QjsRuntime``.

    A single wall-clock interrupt handler is installed at construction.
    Per-eval deadlines are written into ``self._deadline`` by the
    ``Context`` layer before each eval and cleared after; the handler
    reads that slot on every QuickJS interrupt poll and returns True
    once the deadline has elapsed. This is the same design as v0.2's
    bridge — single shared deadline per runtime — now without the
    wasmtime epoch backup (§8 "No wasm epoch interruption").
    """

    def __init__(
        self,
        *,
        memory_limit: int | None = 64 * 1024 * 1024,
        stack_limit: int | None = 1 * 1024 * 1024,
    ) -> None:
        try:
            self._engine_rt = _engine.QjsRuntime(
                memory_limit=memory_limit,
                stack_limit=stack_limit,
            )
        except _engine.QuickJSError as e:
            raise QuickJSError(str(e)) from e

        self._closed = False
        self._contexts: list[Any] = []  # list[Context] once step 2 lands

        # Per-eval deadline slot (monotonic-clock absolute time, seconds).
        # Context writes it before eval, clears it after.
        self._deadline: float | None = None

        def _interrupt() -> bool:
            d = self._deadline
            return d is not None and time.monotonic() >= d

        self._engine_rt.set_interrupt_handler(_interrupt)

    def __enter__(self) -> Runtime:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        for ctx in list(self._contexts):
            ctx.close()
        self._contexts.clear()
        self._engine_rt.close()
        self._closed = True

    def new_context(self, *, timeout: float = 5.0) -> Any:
        if self._closed:
            raise QuickJSError("runtime is closed")
        from quickjs_rs.context import Context

        ctx = Context(self, timeout=timeout)
        self._contexts.append(ctx)
        return ctx

    def _unregister_context(self, ctx: Any) -> None:
        try:
            self._contexts.remove(ctx)
        except ValueError:
            pass

    def run_pending_jobs(self) -> int:
        raise NotImplementedError("run_pending_jobs lands with async support (§7.4).")

    @property
    def has_pending_jobs(self) -> bool:
        raise NotImplementedError("has_pending_jobs lands with async support (§7.4).")
