"""Manual driver primitives for promise/deferred QuickJS execution."""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

import quickjs_rs._engine as _engine
from quickjs_rs.errors import ConcurrentEvalError, QuickJSError
from quickjs_rs.handle import Handle
from quickjs_rs.snapshot import Snapshot
from quickjs_rs.transforms import TransformFlagsProvider

if TYPE_CHECKING:
    from quickjs_rs.context import Context

_HOST_ERROR_SANITIZED_MESSAGE = "Host function failed"


@dataclass(frozen=True, slots=True)
class HostRequest:
    """One promise-boundary host call emitted by the manual driver.

    ``deferred_id`` is the guest resolver key. Resolving/rejecting that id
    settles the JS Promise returned by the host function call.
    """

    deferred_id: int
    fn_id: int
    args: tuple[Any, ...]


class DriverSession:
    """Manual execution driver for a root Promise.

    This is a low-level API for embeddings that need to observe async host calls
    as deferred requests, park them, snapshot the guest, and later resolve the
    corresponding JS Promises themselves. Public ``eval_async`` remains the
    drive-to-completion convenience API.
    """

    def __init__(self, context: Context, root: Handle, deadline: float) -> None:
        self._context = context
        self.root = root
        self._deadline = deadline
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        ctx = self._context
        if ctx._driver_session is self:
            ctx._driver_session = None
            ctx._driver_requests = None
            ctx._runtime._deadline = None
            ctx._eval_async_in_flight = False
        self.root.dispose()

    def __enter__(self) -> DriverSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _require_open(self) -> Context:
        if self._closed:
            raise QuickJSError("driver session is closed")
        if self._context._closed:
            raise QuickJSError("context is closed")
        return self._context

    @property
    def root_handle_id(self) -> int:
        """Raw guest handle id for the root Promise.

        This is intended for same-snapshot checkpoint manifests: after restoring
        the snapshot into another context, :meth:`ContextDriver.handle_from_id`
        can re-wrap this id to inspect/continue the same root Promise.
        """
        return self.root._require_live()._require_live()

    def run_pending_jobs(self) -> int:
        """Drain the QuickJS microtask queue."""
        return self._require_open()._engine_ctx.run_pending_jobs()

    def take_host_requests(self) -> tuple[HostRequest, ...]:
        """Return and clear host requests emitted since the previous call."""
        ctx = self._require_open()
        requests = tuple(ctx._driver_requests or ())
        if ctx._driver_requests is not None:
            ctx._driver_requests.clear()
        return requests

    def promise_state(self) -> Literal["pending", "fulfilled", "rejected"]:
        """Inspect the root promise state."""
        state = self._require_open()._engine_ctx.promise_state(self.root._require_live())
        if state == 0:
            return "pending"
        if state == 1:
            return "fulfilled"
        if state == 2:
            return "rejected"
        raise QuickJSError(f"unknown promise state {state}")

    def promise_result(self) -> Handle:
        """Return the settled root promise result as a handle.

        Call only when :meth:`promise_state` is ``fulfilled`` or ``rejected``.
        For script-mode async eval this is QuickJS's ``{ value, done }``
        envelope, matching the existing internal async eval contract.
        """
        ctx = self._require_open()
        return Handle(ctx, ctx._engine_ctx.promise_result(self.root._require_live()))

    def resolve(self, deferred_id: int, value: Any) -> None:
        """Fulfill a deferred host-call Promise."""
        self._require_open()._engine_ctx.resolve_pending(deferred_id, value)

    def reject(
        self,
        deferred_id: int,
        name: str = "HostError",
        message: str = _HOST_ERROR_SANITIZED_MESSAGE,
        stack: str | None = None,
    ) -> None:
        """Reject a deferred host-call Promise with a JS Error."""
        self._require_open()._engine_ctx.reject_pending(deferred_id, name, message, stack)

    def create_snapshot(self) -> Snapshot:
        """Snapshot a blocked manual-driver context.

        This intentionally allows the root Promise to be pending. It only asserts
        that no Python async host tasks are running; parked deferreds live inside
        the guest heap and are captured by the whole-memory snapshot.
        """
        ctx = self._require_open()
        if ctx._pending_tasks:
            raise QuickJSError("cannot snapshot while async host tasks are pending")
        if ctx._engine_ctx.module_touched:
            raise NotImplementedError(
                "snapshot of a context that ran module=True eval is not supported in V1"
            )
        return Snapshot(ctx._engine_ctx.create_snapshot())


class ContextDriver:
    """Factory for manual driver sessions bound to a :class:`Context`."""

    def __init__(self, context: Context) -> None:
        self._context = context

    def handle_from_id(self, handle_id: int) -> Handle:
        """Wrap a raw guest handle id from a restored snapshot.

        This is a low-level checkpoint/resume primitive. The id must come from
        the same guest heap image currently loaded in this context.
        """
        ctx = self._context
        if ctx._closed:
            raise QuickJSError("context is closed")
        return Handle(
            ctx,
            _engine.QjsHandle._adopt(
                ctx._engine_ctx._inst,
                handle_id,
                context=ctx._engine_ctx,
            ),
        )

    def start_eval(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
        timeout: float | None = None,
        transform_flags: TransformFlagsProvider | None = None,
    ) -> DriverSession:
        """Start async/promise eval and return a manual driver session.

        Async host calls made by this eval are surfaced as :class:`HostRequest`
        objects instead of being scheduled as Python tasks.
        """
        ctx = self._context
        if ctx._closed:
            raise QuickJSError("context is closed")
        if ctx._eval_async_in_flight:
            raise ConcurrentEvalError(
                "another eval_async is already in flight on this context; use a separate "
                "context for concurrent JS workloads"
            )
        if ctx._driver_session is not None:
            raise ConcurrentEvalError("another driver session is already active on this context")
        deadline = time.monotonic() + (timeout if timeout is not None else ctx._timeout)
        ctx._last_host_exception = None
        ctx._driver_requests = []
        ctx._eval_async_in_flight = True
        ctx._runtime._deadline = deadline
        try:
            root = ctx._eval_for_async(
                code,
                module,
                strict,
                filename,
                deadline,
                transform_flags,
            )
            session = DriverSession(ctx, root, deadline)
            ctx._driver_session = session
            return session
        except BaseException:
            ctx._driver_requests = None
            ctx._runtime._deadline = None
            ctx._eval_async_in_flight = False
            raise
