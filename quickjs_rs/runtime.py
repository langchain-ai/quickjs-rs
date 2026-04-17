"""Runtime. See spec/implementation.md §7.2."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING

from quickjs_rs._bridge import Bridge
from quickjs_rs.errors import QuickJSError

if TYPE_CHECKING:
    from quickjs_rs.context import Context


class Runtime:
    """Owns the JS heap, memory/stack limits, and the wasm instance.

    Internals worth knowing for tuning:

    - A daemon thread per Runtime increments the wasmtime engine's epoch
      at a fixed 50 ms cadence. This is what gives ``set_epoch_deadline``
      (§9's trap-based backup to ``host_interrupt``) a clock to race
      against. Don't lower the cadence without understanding what it's
      protecting against: the wall-clock interrupt path is primary, and
      the epoch backup only matters when QuickJS is in a C-level loop
      that doesn't poll interrupts. 50 ms gives ~100 ms worst-case lag
      on that already-catastrophic scenario, which is acceptable; 5 ms
      would cut lag at the cost of 10× the per-tick wakeups for the
      common case where nothing has gone wrong.
    """

    def __init__(
        self,
        *,
        memory_limit: int | None = 64 * 1024 * 1024,
        stack_limit: int | None = 1 * 1024 * 1024,
    ) -> None:
        self._bridge = Bridge()
        rt_id = self._bridge.runtime_new()
        if rt_id == 0:
            raise QuickJSError("failed to create QuickJS runtime")
        self._rt_id = rt_id
        self._closed = False
        self._contexts: list[Context] = []

        if memory_limit is not None:
            self._bridge.runtime_set_memory_limit(rt_id, memory_limit)
        if stack_limit is not None:
            self._bridge.runtime_set_stack_limit(rt_id, stack_limit)
        self._bridge.runtime_install_interrupt(rt_id)

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
        # Close contexts first (§7.3).
        for ctx in list(self._contexts):
            ctx.close()
        self._contexts.clear()
        self._bridge.runtime_free(self._rt_id)
        self._bridge.close()
        self._closed = True

    def new_context(self, *, timeout: float = 5.0) -> Context:
        if self._closed:
            raise QuickJSError("runtime is closed")
        from quickjs_rs.context import Context

        ctx = Context(self, timeout=timeout)
        self._contexts.append(ctx)
        return ctx

    def _unregister_context(self, ctx: Context) -> None:
        try:
            self._contexts.remove(ctx)
        except ValueError:
            pass

    # ---- Public job helpers --------------------------------------------

    def run_pending_jobs(self) -> int:
        raise NotImplementedError("run_pending_jobs lands with async support (§7.2).")

    @property
    def has_pending_jobs(self) -> bool:
        raise NotImplementedError("has_pending_jobs lands with async support (§7.2).")
