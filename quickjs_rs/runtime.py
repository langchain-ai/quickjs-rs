"""Runtime. See README.md"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import TracebackType
from typing import Any

import quickjs_rs._engine as _engine
from quickjs_rs.errors import QuickJSError
from quickjs_rs.snapshot import Snapshot


def _validate_runtime_context(runtime: Runtime, ctx: Any) -> None:
    """Validate runtime/context ownership and lifecycle preconditions."""
    if runtime._closed:
        raise QuickJSError("runtime is closed")
    if not hasattr(ctx, "_engine_ctx"):
        raise TypeError("ctx must be a quickjs_rs.Context")
    if ctx._closed:
        raise QuickJSError("context is closed")
    if ctx._runtime is not runtime:
        raise QuickJSError("context belongs to a different runtime")


class Runtime:
    """Owns the QuickJS runtime (JS heap, memory/stack limits, interrupt
    handler). Thin wrapper over ``_engine.QjsRuntime``.

    A single wall-clock interrupt handler is installed at construction.
    Per-eval deadlines are written into ``self._deadline`` by the
    ``Context`` layer before each eval and cleared after; the handler
    reads that slot on every QuickJS interrupt poll and returns True
    once the deadline has elapsed.
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

    def run_gc(self) -> None:
        """Run QuickJS cycle GC on this runtime."""
        if self._closed:
            raise QuickJSError("runtime is closed")
        self._engine_rt.run_gc()

    def memory_usage(self) -> dict[str, int]:
        """Return QuickJS runtime memory counters from JS_ComputeMemoryUsage."""
        if self._closed:
            raise QuickJSError("runtime is closed")
        raw = self._engine_rt.memory_usage()
        return {str(k): int(v) for k, v in raw.items()}

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

    def set_module_loader(
        self,
        *,
        normalize: Callable[[str, str], str | None] | None = None,
        load: Callable[[str], str | None],
    ) -> None:
        """Install a host module loader for ES `import`/`export` resolution
        (quickjs-wasi `moduleLoader` shape). The loader is shared by every
        context on this runtime.

        Args:
            normalize: ``normalize(base, specifier) -> canonical_name``. Given
                the importing module's name (``base``) and the import specifier,
                return the canonical module name. Optional — the default passes
                the specifier through unchanged. Return ``None`` for an
                unresolvable specifier. The host owns ALL resolution policy here
                (relative-path joining, sandboxing, aliasing): there is no
                built-in scope model.
            load: ``load(canonical_name) -> source``. Return the module source
                (a string) for a canonical name, or ``None`` if not found.
                Required.

        Static imports are resolved synchronously at module instantiation, so
        these callbacks are invoked synchronously by the guest. They run on the
        calling thread during eval.
        """
        if self._closed:
            raise QuickJSError("runtime is closed")
        self._engine_rt.set_module_loader(normalize=normalize, load=load)

    def restore_snapshot(
        self,
        snapshot: Snapshot,
        ctx: Any,
        *,
        inject_globals: bool = True,
    ) -> None:
        """Restore a WHOLE-MEMORY snapshot into ``ctx``.

        The destination context's entire guest heap is replaced by the
        snapshot image — every object, closure, pending promise, and aliasing
        relationship is reconstituted. The header is validated fail-closed
        (magic → format version → build_id → size → stack pointer) before any
        byte is written; a snapshot from a different guest build is rejected.

        Args:
            snapshot: Snapshot produced by :meth:`create_snapshot` /
                :meth:`Context.create_snapshot` (or the async variants).
            ctx: Destination context (must belong to this runtime).
            inject_globals: ``True`` (default) writes the image. ``False``
                validates the header only and does NOT mutate the destination
                (a whole-memory restore replaces ALL state wholesale, so there
                is no partial "inject just the globals" — the flag means
                validate-only vs apply).

        Raises:
            TypeError: If ``snapshot`` is not a :class:`quickjs_rs.Snapshot`.
            QuickJSError: If the runtime/context is closed or the context
                belongs to a different runtime.
            ValueError: If the snapshot header is malformed, its format version
                is unsupported, or its ``build_id`` does not match this guest
                build.
        """
        _validate_runtime_context(self, ctx)
        if not isinstance(snapshot, Snapshot):
            raise TypeError("snapshot must be quickjs_rs.Snapshot")
        try:
            ctx._engine_ctx.restore_snapshot(snapshot.to_bytes(), write=inject_globals)
        except _engine.QuickJSError as e:
            raise QuickJSError(str(e)) from None

    def create_snapshot(self, ctx: Any) -> Snapshot:
        """Capture a whole-memory snapshot of ``ctx`` (factory form of
        :meth:`Context.create_snapshot`). See it for semantics."""
        _validate_runtime_context(self, ctx)
        return ctx.create_snapshot()

    async def create_snapshot_async(self, ctx: Any, *, timeout: float | None = None) -> Snapshot:
        """Async factory form of :meth:`Context.create_snapshot_async`."""
        _validate_runtime_context(self, ctx)
        return await ctx.create_snapshot_async(timeout=timeout)
