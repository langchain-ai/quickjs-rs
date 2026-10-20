"""Bounded snapshot deltas through an isolated qbsdiff WASM guest."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from contextlib import ExitStack, suppress
from importlib.resources import files

import wasmtime

from quickjs_rs.errors import QuickJSError

_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_MEMORY_LIMIT = 1024 * 1024 * 1024
_DEFAULT_FUEL = 100_000_000_000
_MAX_BYTES = 256 * 1024 * 1024
_ARTIFACT_LOCK = threading.Lock()
_ARTIFACT: tuple[wasmtime.Engine, wasmtime.Module] | None = None


class SnapshotDiffError(QuickJSError):
    """Snapshot delta validation, execution, or resource limits failed."""


def snapshot_diff(
    source: bytes,
    target: bytes,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    memory_limit: int = _DEFAULT_MEMORY_LIMIT,
    fuel: int = _DEFAULT_FUEL,
) -> bytes:
    """Create a bounded BSDIFF40 delta; see README for limits and compatibility."""
    return _operate("diff", source, target, max_bytes, memory_limit, fuel)


def snapshot_patch(
    source: bytes,
    patch: bytes,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    memory_limit: int = _DEFAULT_MEMORY_LIMIT,
    fuel: int = _DEFAULT_FUEL,
) -> bytes:
    """Apply a bounded BSDIFF40 delta, rejecting unsupported source references."""
    return _operate("patch", source, patch, max_bytes, memory_limit, fuel)


def _bounded_int(name: str, value: int, upper: int) -> None:
    if type(value) is not int or not 0 < value <= upper:
        raise SnapshotDiffError(f"{name} must be an integer in [1, {upper}]")


def _bounded_bytes(name: str, data: bytes, upper: int) -> None:
    if type(data) is not bytes:
        raise SnapshotDiffError(f"{name} must be bytes")
    if len(data) > upper:
        raise SnapshotDiffError(f"{name} exceeds the {upper}-byte limit")


def _artifact() -> tuple[wasmtime.Engine, wasmtime.Module]:
    global _ARTIFACT
    with _ARTIFACT_LOCK:
        if _ARTIFACT is None:
            resource = files("quickjs_rs") / "_diff.wasm"
            if not resource.is_file():
                raise SnapshotDiffError(
                    "snapshot diff wasm not found (quickjs_rs/_diff.wasm). "
                    "Build it with `python scripts/build_guest.py`."
                )
            with wasmtime.Config() as config:
                config.consume_fuel = True
                engine = wasmtime.Engine(config)
            try:
                module = wasmtime.Module(engine, resource.read_bytes())
            except BaseException:
                with suppress(Exception):
                    engine.close()
                raise
            _ARTIFACT = engine, module
        return _ARTIFACT


def _close(resource: wasmtime.Store | wasmtime.Linker) -> None:
    with suppress(Exception):
        resource.close()


def _call(exports: Mapping[str, object], store: wasmtime.Store, name: str, *args: int) -> int:
    function = exports.get(name)
    if not isinstance(function, wasmtime.Func):
        raise SnapshotDiffError(f"missing guest function: {name}")
    value = function(store, *args)
    if type(value) is not int or not -(1 << 31) <= value < (1 << 31):
        raise SnapshotDiffError(f"invalid guest return value: {name}")
    return value & 0xFFFFFFFF


def _check_range(memory: wasmtime.Memory, store: wasmtime.Store, ptr: int, length: int) -> None:
    if (length and ptr == 0) or ptr + length > memory.data_len(store):
        raise SnapshotDiffError("guest memory range is out of bounds")


def _alloc_write(
    exports: Mapping[str, object],
    memory: wasmtime.Memory,
    store: wasmtime.Store,
    data: bytes,
) -> int:
    ptr = _call(exports, store, "qjsd_alloc", len(data))
    if ptr == 0:
        raise SnapshotDiffError("guest allocation failed")
    _check_range(memory, store, ptr, len(data))
    if data:
        memory.write(store, data, ptr)
    return ptr


def _read(
    exports: Mapping[str, object],
    memory: wasmtime.Memory,
    store: wasmtime.Store,
    kind: str,
    limit: int,
) -> bytes:
    length = _call(exports, store, f"qjsd_{kind}_len")
    if length > limit:
        raise SnapshotDiffError(f"guest {kind} exceeds the {limit}-byte limit")
    ptr = _call(exports, store, f"qjsd_{kind}_ptr")
    _check_range(memory, store, ptr, length)
    return bytes(memory.read(store, ptr, ptr + length)) if length else b""


def _operate(
    operation: str,
    source: bytes,
    other: bytes,
    max_bytes: int,
    memory_limit: int,
    fuel: int,
) -> bytes:
    """Destroy the whole store, including input/result allocations, even after traps."""
    _bounded_int("max_bytes", max_bytes, _MAX_BYTES)
    _bounded_int("memory_limit", memory_limit, 1 << 32)
    _bounded_int("fuel", fuel, (1 << 64) - 1)
    patch_limit = 2 * max_bytes + 4096
    _bounded_bytes("source", source, max_bytes)
    _bounded_bytes(
        "target" if operation == "diff" else "patch",
        other,
        max_bytes if operation == "diff" else patch_limit,
    )
    output_limit = patch_limit if operation == "diff" else max_bytes
    try:
        engine, module = _artifact()
        with ExitStack() as cleanup:
            store = wasmtime.Store(engine)
            cleanup.callback(_close, store)
            store.set_limits(memory_size=memory_limit)
            store.set_fuel(fuel)
            with wasmtime.WasiConfig() as wasi:
                store.set_wasi(wasi)
            linker = wasmtime.Linker(engine)
            cleanup.callback(_close, linker)
            linker.define_wasi()
            instance = linker.instantiate(store, module)
            exports = instance.exports(store)
            memory = exports.get("memory")
            if not isinstance(memory, wasmtime.Memory):
                raise SnapshotDiffError("guest memory export is missing")
            source_ptr = _alloc_write(exports, memory, store, source)
            other_ptr = _alloc_write(exports, memory, store, other)
            status = _call(
                exports,
                store,
                f"qjsd_{operation}",
                source_ptr,
                len(source),
                other_ptr,
                len(other),
                output_limit,
            )
            if status != 0:
                error = _read(exports, memory, store, "error", 4096).decode(errors="replace")
                raise SnapshotDiffError(error or f"snapshot {operation} failed (status {status})")
            return _read(exports, memory, store, "result", output_limit)
    except (
        wasmtime.WasmtimeError,
        wasmtime.Trap,
        OSError,
        MemoryError,
        ValueError,
        TypeError,
        OverflowError,
    ) as exc:
        raise SnapshotDiffError(f"snapshot {operation} failed: {exc}") from exc
