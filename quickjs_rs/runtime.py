"""Runtime. See README.md section 7."""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import QuickJSError
from quickjs_rs.modules import ModuleScope


class Runtime:
    """Owns the QuickJS runtime (JS heap, memory/stack limits, interrupt
    handler). Thin wrapper over ``_engine.QjsRuntime``.

    A single wall-clock interrupt handler is installed at construction.
    Per-eval deadlines are written into ``self._deadline`` by the
    ``Context`` layer before each eval and cleared after; the handler
    reads that slot on every QuickJS interrupt poll and returns True
    once the deadline has elapsed. This is the same design as previous implementation's
    bridge — single shared deadline per runtime — now without the
    wasmtime epoch backup (section 8 "No wasm epoch interruption").
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

    def install(self, scope: ModuleScope) -> None:
        """Register modules in this runtime's shared module store.

        Any context created from this runtime can import installed
        modules (subject to QuickJS module-cache semantics).
        """
        if self._closed:
            raise QuickJSError("runtime is closed")
        self._install_recursive(scope, scope_path="")

    def _install_recursive(self, scope: ModuleScope, scope_path: str) -> None:
        for key, value in scope.modules.items():
            if isinstance(value, str):
                canonical = key if scope_path == "" else f"{scope_path}/{key}"
                try:
                    self._engine_rt.add_module_source(
                        scope_path, key, canonical, value
                    )
                except _engine.QuickJSError as e:
                    # section 5.5: TypeScript parse errors surface at install.
                    raise QuickJSError(str(e)) from e
            elif isinstance(value, ModuleScope):
                self._engine_rt.register_subscope(scope_path, key)
                child_path = key if scope_path == "" else f"{scope_path}/{key}"
                self._install_recursive(value, scope_path=child_path)
            else:
                raise TypeError(
                    f"ModuleScope entry {key!r}: expected str | ModuleScope, "
                    f"got {type(value).__name__}"
                )

    def run_pending_jobs(self) -> int:
        raise NotImplementedError("run_pending_jobs lands with async support (section 7.4).")

    @property
    def has_pending_jobs(self) -> bool:
        raise NotImplementedError("has_pending_jobs lands with async support (section 7.4).")
