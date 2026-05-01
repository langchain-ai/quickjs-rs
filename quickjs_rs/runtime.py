"""Runtime. See README.md"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, TypeAlias

import quickjs_rs._engine as _engine
from quickjs_rs.errors import ConcurrentEvalError, QuickJSError
from quickjs_rs.modules import ModuleScope
from quickjs_rs.snapshot import Snapshot

_SnapshotResolveStatus: TypeAlias = Literal["active", "missing", "unserializable"]
_ResolvedEntry: TypeAlias = tuple[str, _SnapshotResolveStatus, _engine.QjsHandle | None, str | None]
_ClassifiedResolvedHandle: TypeAlias = tuple[_ResolvedEntry, Any | None]


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


def _validate_snapshot_request(
    runtime: Runtime,
    ctx: Any,
    *,
    on_unserializable: str,
    on_missing_name: str,
) -> None:
    """Validate snapshot-specific preconditions and policy option values."""
    _validate_runtime_context(runtime, ctx)
    if ctx._eval_async_in_flight:
        raise ConcurrentEvalError("create_snapshot() cannot run while eval_async is in flight")
    if ctx._pending_tasks:
        raise QuickJSError("create_snapshot() cannot run while async host tasks are pending")
    if on_unserializable not in {"tombstone", "error"}:
        raise ValueError("on_unserializable must be 'tombstone' or 'error'")
    if on_missing_name not in {"skip", "tombstone", "error"}:
        raise ValueError("on_missing_name must be 'skip', 'tombstone', or 'error'")
    if ctx._engine_ctx.snapshot_module_touched():
        raise NotImplementedError(
            "create_snapshot() is not implemented for contexts that "
            "executed module=True eval; module-mode snapshotting is not implemented in V1"
        )


def _classify_resolved_snapshot_handle(
    ctx: Any,
    *,
    name: str,
    handle: Any,
    allow_bytecode: bool,
    allow_reference: bool,
    allow_sab: bool,
) -> _ClassifiedResolvedHandle:
    """Classify a resolved handle as active or unserializable.

    Returns an entry tuple for the engine's resolved-entry contract and,
    when active, the live handle to retain until snapshot finalization.
    """
    type_name = handle.type_of
    try:
        ctx._engine_ctx.dump_handle(
            handle._require_live(),
            allow_bytecode=allow_bytecode,
            allow_reference=allow_reference,
            allow_sab=allow_sab,
        )
    except (_engine.JSError, _engine.QuickJSError, _engine.MarshalError):
        handle.dispose()
        return (name, "unserializable", None, type_name), None

    return (name, "active", handle._require_live(), None), handle


def _record_missing_snapshot_name(
    *,
    name: str,
    on_missing_name: str,
    error: QuickJSError,
) -> _ResolvedEntry:
    """Map missing-name resolution outcome to a resolved-entry tuple."""
    if on_missing_name == "error":
        raise error
    return (name, "missing", None, None)


@dataclass
class _SnapshotResolution:
    """Container for resolved snapshot entries and live active handles."""

    entries: list[_ResolvedEntry]
    active_handles: list[Any]

    def dispose(self) -> None:
        """Dispose all retained active handles after snapshot finalization."""
        for handle in self.active_handles:
            handle.dispose()


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
                    self._engine_rt.add_module_source(scope_path, key, canonical, value)
                except _engine.QuickJSError as e:
                    # TypeScript parse errors surface at install.
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

    def restore_snapshot(
        self,
        snapshot: Snapshot,
        ctx: Any,
        *,
        inject_globals: bool = True,
    ) -> None:
        """Restore a snapshot payload into ``ctx``.

        Args:
            snapshot: Snapshot payload previously produced by
                :meth:`create_snapshot`,
                :meth:`create_snapshot_async`, or the corresponding
                context helpers.
            ctx: Destination context. It must belong to this runtime.
            inject_globals: If ``True`` (default), restored active
                names are injected into the destination context's
                globals and tombstones are installed for unavailable
                names. If ``False``, the payload is decoded and
                validated without mutating the destination globals.

        Raises:
            TypeError: If ``snapshot`` is not a
                :class:`quickjs_rs.Snapshot`.
            QuickJSError: If the runtime or context is closed, the
                context belongs to a different runtime, or the payload
                cannot be restored.
            ValueError: If the snapshot envelope is malformed or its
                compatibility metadata does not match the current
                runtime.
        """
        _validate_runtime_context(self, ctx)
        if not isinstance(snapshot, Snapshot):
            raise TypeError("snapshot must be quickjs_rs.Snapshot")
        try:
            ctx._engine_ctx.restore_snapshot_bytes(
                snapshot.to_bytes(),
                inject_globals=inject_globals,
            )
        except _engine.QuickJSError as e:
            raise QuickJSError(str(e)) from None

    def create_snapshot(
        self,
        ctx: Any,
        *,
        on_unserializable: str = "tombstone",
        on_missing_name: str = "skip",
        allow_bytecode: bool = False,
        allow_reference: bool = True,
        allow_sab: bool = False,
    ) -> Snapshot:
        """Create a snapshot from ``ctx`` using synchronous resolution.

        Args:
            ctx: Source context. It must belong to this runtime.
            on_unserializable: Policy for tracked names whose resolved
                value cannot be serialized. ``"tombstone"`` records the
                name as unavailable after restore; ``"error"`` fails
                snapshot creation.
            on_missing_name: Policy for tracked names that no longer
                resolve by identifier lookup at snapshot time.
                ``"skip"`` omits the name entirely, ``"tombstone"``
                restores a throwing placeholder, and ``"error"`` fails
                snapshot creation.
            allow_bytecode: Passed through to QuickJS object
                serialization.
            allow_reference: Passed through to QuickJS object
                serialization. Enabled by default so shared references
                inside the captured graph can round-trip.
            allow_sab: Passed through to QuickJS object serialization
                for SharedArrayBuffer support.

        Returns:
            A :class:`quickjs_rs.Snapshot` payload.

        Raises:
            NotImplementedError: If ``ctx`` has executed any
                ``module=True`` eval surface.
            ConcurrentEvalError: If ``eval_async`` is currently in
                flight on ``ctx``.
            QuickJSError: If the runtime/context is closed, the context
                belongs to a different runtime, async host tasks are
                pending, or policy requires an unavailable name to fail
                snapshot creation.
        """
        _validate_snapshot_request(
            self,
            ctx,
            on_unserializable=on_unserializable,
            on_missing_name=on_missing_name,
        )
        resolution: _SnapshotResolution | None = None
        try:
            resolution = self._resolve_snapshot_entries_sync(
                ctx,
                on_missing_name=on_missing_name,
                allow_bytecode=allow_bytecode,
                allow_reference=allow_reference,
                allow_sab=allow_sab,
            )
            return self._create_snapshot_from_resolved(
                ctx,
                resolved_entries=resolution.entries,
                on_unserializable=on_unserializable,
                on_missing_name=on_missing_name,
                allow_bytecode=allow_bytecode,
                allow_reference=allow_reference,
                allow_sab=allow_sab,
            )
        finally:
            if resolution is not None:
                resolution.dispose()

    async def create_snapshot_async(
        self,
        ctx: Any,
        *,
        on_unserializable: str = "tombstone",
        on_missing_name: str = "skip",
        allow_bytecode: bool = False,
        allow_reference: bool = True,
        allow_sab: bool = False,
        timeout: float | None = None,
    ) -> Snapshot:
        """Create a snapshot from ``ctx`` using async resolution.

        Args:
            ctx: Source context. It must belong to this runtime.
            on_unserializable: See :meth:`create_snapshot`.
            on_missing_name: See :meth:`create_snapshot`.
            allow_bytecode: See :meth:`create_snapshot`.
            allow_reference: See :meth:`create_snapshot`.
            allow_sab: See :meth:`create_snapshot`.
            timeout: Optional per-name timeout override passed to
                ``ctx.eval_handle_async(...)`` while resolving tracked
                names. ``None`` uses the context's cumulative async
                timeout behavior.

        Returns:
            A :class:`quickjs_rs.Snapshot` payload.

        Raises:
            NotImplementedError: If ``ctx`` has executed any
                ``module=True`` eval surface.
            ConcurrentEvalError: If ``eval_async`` is currently in
                flight on ``ctx``.
            QuickJSError: If the runtime/context is closed, the context
                belongs to a different runtime, async host tasks are
                pending, or policy requires an unavailable name to fail
                snapshot creation.
        """
        _validate_snapshot_request(
            self,
            ctx,
            on_unserializable=on_unserializable,
            on_missing_name=on_missing_name,
        )
        resolution: _SnapshotResolution | None = None
        try:
            resolution = await self._resolve_snapshot_entries_async(
                ctx,
                on_missing_name=on_missing_name,
                allow_bytecode=allow_bytecode,
                allow_reference=allow_reference,
                allow_sab=allow_sab,
                timeout=timeout,
            )
            return self._create_snapshot_from_resolved(
                ctx,
                resolved_entries=resolution.entries,
                on_unserializable=on_unserializable,
                on_missing_name=on_missing_name,
                allow_bytecode=allow_bytecode,
                allow_reference=allow_reference,
                allow_sab=allow_sab,
            )
        finally:
            if resolution is not None:
                resolution.dispose()

    def _resolve_snapshot_entries_sync(
        self,
        ctx: Any,
        *,
        on_missing_name: str,
        allow_bytecode: bool,
        allow_reference: bool,
        allow_sab: bool,
    ) -> _SnapshotResolution:
        names = list(ctx._engine_ctx.snapshot_registry_names())
        resolved_entries: list[_ResolvedEntry] = []
        active_handles: list[Any] = []
        for name in names:
            try:
                handle = ctx.eval_handle(name)
            except QuickJSError as e:
                resolved_entries.append(
                    _record_missing_snapshot_name(
                        name=name,
                        on_missing_name=on_missing_name,
                        error=e,
                    )
                )
                continue
            entry, active_handle = _classify_resolved_snapshot_handle(
                ctx,
                name=name,
                handle=handle,
                allow_bytecode=allow_bytecode,
                allow_reference=allow_reference,
                allow_sab=allow_sab,
            )
            resolved_entries.append(entry)
            if active_handle is not None:
                active_handles.append(active_handle)
        return _SnapshotResolution(entries=resolved_entries, active_handles=active_handles)

    async def _resolve_snapshot_entries_async(
        self,
        ctx: Any,
        *,
        on_missing_name: str,
        allow_bytecode: bool,
        allow_reference: bool,
        allow_sab: bool,
        timeout: float | None,
    ) -> _SnapshotResolution:
        names = list(ctx._engine_ctx.snapshot_registry_names())
        resolved_entries: list[_ResolvedEntry] = []
        active_handles: list[Any] = []
        for name in names:
            try:
                handle = await ctx.eval_handle_async(name, timeout=timeout)
            except QuickJSError as e:
                resolved_entries.append(
                    _record_missing_snapshot_name(
                        name=name,
                        on_missing_name=on_missing_name,
                        error=e,
                    )
                )
                continue
            entry, active_handle = _classify_resolved_snapshot_handle(
                ctx,
                name=name,
                handle=handle,
                allow_bytecode=allow_bytecode,
                allow_reference=allow_reference,
                allow_sab=allow_sab,
            )
            resolved_entries.append(entry)
            if active_handle is not None:
                active_handles.append(active_handle)
        return _SnapshotResolution(entries=resolved_entries, active_handles=active_handles)

    def _create_snapshot_from_resolved(
        self,
        ctx: Any,
        *,
        resolved_entries: list[_ResolvedEntry],
        on_unserializable: str,
        on_missing_name: str,
        allow_bytecode: bool,
        allow_reference: bool,
        allow_sab: bool,
    ) -> Snapshot:
        try:
            blob = ctx._engine_ctx.create_snapshot_from_resolved(
                resolved_entries=resolved_entries,
                on_unserializable=on_unserializable,
                on_missing_name=on_missing_name,
                allow_bytecode=allow_bytecode,
                allow_reference=allow_reference,
                allow_sab=allow_sab,
            )
        except _engine.JSError as e:
            name, message, stack = e.args
            classified = ctx._classify_jserror(name, message, stack, None)
            raise classified from classified.__cause__
        except _engine.QuickJSError as e:
            raise QuickJSError(str(e)) from None
        return Snapshot(blob)
