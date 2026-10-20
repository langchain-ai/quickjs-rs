"""Public snapshot delta API and fail-closed guest boundary tests."""

from __future__ import annotations

import bz2
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import wasmtime

from quickjs_rs import QuickJSError, SnapshotDiffError, _diff, snapshot_diff, snapshot_patch
from quickjs_rs._wasmtime import shared_wasmtime_engine

_GOLDEN_SOURCE = b"The quick brown fox jumps over the lazy dog.\n"
_GOLDEN_TARGET = b"The quick red fox jumps over the very lazy dog!\n"
_GOLDEN_PATCH = bytes.fromhex(
    "4253444946463430300000000000000027000000000000003000000000000000"
    "425a68393141592653594a6fb330000010e0004a3084002000310c00910c9d925b"
    "441189a3c5dc914e1424129beccc00"
    "425a6839314159265359ad7c200000000060004000001020002100828317724538"
    "5090ad7c2000"
    "425a6839314159265359979ba31d0000065180001060002684913020003100d001"
    "00034282ab8bd01c6caf5778bb9229c28484bcdd18e8"
)


def _offset(value: int) -> bytes:
    return (abs(value) | ((1 << 63) if value < 0 else 0)).to_bytes(8, "little")


def _patch(controls: list[tuple[int, int, int]], diff: bytes, extra: bytes, size: int) -> bytes:
    control = bz2.compress(b"".join(_offset(v) for triple in controls for v in triple))
    delta = bz2.compress(diff)
    return (
        b"BSDIFF40"
        + _offset(len(control))
        + _offset(len(delta))
        + _offset(size)
        + control
        + delta
        + bz2.compress(extra)
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (b"", b""),
        (b"", b"new bytes"),
        (b"removed bytes", b""),
        (b"unchanged", b"unchanged"),
        (b"old", b"a longer replacement"),
        (b"a longer original", b"new"),
        (bytes(range(256)) * 8, bytes(range(255, -1, -1)) * 8),
        (b"\x00" * 8192, b"\x00" * 4096 + b"changed" + b"\x00" * 4089),
    ],
)
def test_roundtrip_and_repeatability(source: bytes, target: bytes) -> None:
    patch = snapshot_diff(source, target)
    assert patch.startswith(b"BSDIFF40")
    assert type(patch) is bytes
    assert snapshot_patch(source, patch) == target
    assert snapshot_diff(source, target) == patch


def test_external_bsdiff4_golden() -> None:
    """Patch generated externally with bsdiff4 1.2.6, not with this guest."""
    assert snapshot_patch(_GOLDEN_SOURCE, _GOLDEN_PATCH) == _GOLDEN_TARGET


def test_error_is_public_quickjs_error() -> None:
    assert issubclass(SnapshotDiffError, QuickJSError)


@pytest.mark.parametrize("operation", [snapshot_diff, snapshot_patch])
@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("value", [None, "text", 123, bytearray(b"abc"), memoryview(b"abc")])
def test_inputs_require_bytes_before_guest(
    operation: Any,
    position: int,
    value: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_guest() -> None:
        pytest.fail("invalid arguments reached guest compilation")

    monkeypatch.setattr(_diff, "_artifact", unexpected_guest)
    args = [b"source", b"target"]
    args[position] = value
    with pytest.raises(SnapshotDiffError, match="must be bytes"):
        operation(*args)


@pytest.mark.parametrize("operation", [snapshot_diff, snapshot_patch])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        (name, value)
        for name in ("max_bytes", "memory_limit", "fuel")
        for value in (False, True, None, "1", 1.0, -1, 0)
    ]
    + [("max_bytes", 256 * 1024 * 1024 + 1), ("memory_limit", (1 << 32) + 1), ("fuel", 1 << 64)],
)
def test_invalid_options_before_guest(
    operation: Any,
    name: str,
    value: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_guest() -> None:
        pytest.fail("invalid options reached guest compilation")

    monkeypatch.setattr(_diff, "_artifact", unexpected_guest)
    with pytest.raises(SnapshotDiffError, match=name):
        operation(b"", b"", **{name: value})


@pytest.mark.parametrize(
    ("operation", "source", "other", "limit"),
    [
        (snapshot_diff, b"123", b"", 2),
        (snapshot_diff, b"", b"123", 2),
        (snapshot_patch, b"123", b"", 2),
        (snapshot_patch, b"", b"x" * 4101, 2),
    ],
)
def test_input_caps_before_guest(
    operation: Any,
    source: bytes,
    other: bytes,
    limit: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_guest() -> None:
        pytest.fail("oversized input reached guest compilation")

    monkeypatch.setattr(_diff, "_artifact", unexpected_guest)
    with pytest.raises(SnapshotDiffError, match="exceeds"):
        operation(source, other, max_bytes=limit)


def test_exact_input_and_output_cap() -> None:
    patch = snapshot_diff(b"a", b"b", max_bytes=1)
    assert len(patch) <= 4098
    assert snapshot_patch(b"a", patch, max_bytes=1) == b"b"


@pytest.mark.parametrize(
    "patch",
    [
        b"",
        b"BSDIFF40",
        b"NOTDIFF!" + b"\x00" * 24,
        b"BSDIFF40" + _offset(-1) + _offset(0) + _offset(1),
        b"BSDIFF40" + _offset(0) + _offset(-1) + _offset(1),
        b"BSDIFF40" + _offset(0) + _offset(0) + _offset(-1),
        b"BSDIFF40" + _offset((1 << 63) - 1) * 3,
        _GOLDEN_PATCH[:40],
        _GOLDEN_PATCH[:32] + b"corrupted compressed body",
        _patch([(0, 1, 0)], b"", b"x", 2),
        _patch([(0, 2, 0)], b"", b"xy", 1),
        _patch([(-1, 1, 0)], b"", b"x", 1),
        _patch([(1, -1, 0)], b"\x00", b"", 1),
    ],
)
def test_malformed_patches_fail_closed(patch: bytes) -> None:
    with pytest.raises(SnapshotDiffError):
        snapshot_patch(_GOLDEN_SOURCE, patch)


def test_patch_target_cap() -> None:
    patch = _patch([(0, 2, 0)], b"", b"ab", 2)
    with pytest.raises(SnapshotDiffError):
        snapshot_patch(b"", patch, max_bytes=1)
    assert snapshot_patch(b"", patch, max_bytes=2) == b"ab"


def test_source_out_of_bounds_reference_is_not_supported() -> None:
    """The baseline bsdiff4 reference yields b'A\\x00'; qbsdiff rejects this case."""
    patch = _patch([(2, 0, 0)], b"\x00\x00", b"", 2)
    with pytest.raises(SnapshotDiffError):
        snapshot_patch(b"A", patch)


@pytest.mark.parametrize("operation", [snapshot_diff, snapshot_patch])
@pytest.mark.parametrize("options", [{"fuel": 1}, {"memory_limit": 1}])
def test_resource_failure_then_recovery(operation: Any, options: dict[str, int]) -> None:
    other = b"target" if operation is snapshot_diff else _GOLDEN_PATCH
    source = b"source" if operation is snapshot_diff else _GOLDEN_SOURCE
    with pytest.raises(SnapshotDiffError):
        operation(source, other, **options)
    assert snapshot_patch(_GOLDEN_SOURCE, _GOLDEN_PATCH) == _GOLDEN_TARGET
    assert snapshot_patch(b"a", snapshot_diff(b"a", b"b")) == b"b"


def test_memory_exhaustion_during_allocation_then_recovery() -> None:
    with pytest.raises(SnapshotDiffError):
        snapshot_diff(b"a" * (2 * 1024 * 1024), b"b", memory_limit=2 * 1024 * 1024)
    assert snapshot_patch(b"a", snapshot_diff(b"a", b"b")) == b"b"


def test_compressed_zero_progress_is_fuel_bounded() -> None:
    patch = _patch([(0, 0, 0)] * 10000, b"", b"", 1)
    with pytest.raises(SnapshotDiffError):
        snapshot_patch(b"", patch, fuel=100000)
    assert snapshot_patch(_GOLDEN_SOURCE, _GOLDEN_PATCH) == _GOLDEN_TARGET


def test_parallel_calls_have_independent_state() -> None:
    def roundtrip(index: int) -> bytes:
        source = bytes([index]) * 1024
        target = source[:512] + f"target {index}".encode() + source[512:]
        assert snapshot_patch(source, snapshot_diff(source, target)) == target
        return target

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(roundtrip, range(12)))
    assert len(set(results)) == 12


def test_diff_engine_is_separate_and_module_cached() -> None:
    first = _diff._artifact()
    assert _diff._artifact() is first
    assert first[0] is not shared_wasmtime_engine()


@pytest.fixture
def hostile_guest(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    artifacts: list[tuple[wasmtime.Engine, wasmtime.Module]] = []

    def install(
        alloc: int = 1024,
        ptr: int = 1024,
        length: int = 1,
        status: int = 0,
        body: str | None = None,
    ) -> None:
        with wasmtime.Config() as config:
            config.consume_fuel = True
            engine = wasmtime.Engine(config)
        module = wasmtime.Module(
            engine,
            f"""(module
            (memory (export "memory") 1)
            (func (export "qjsd_alloc") (param i32) (result i32) i32.const {alloc})
            (func (export "qjsd_diff") (param i32 i32 i32 i32 i32) (result i32)
                {body if body is not None else f"i32.const {status}"})
            (func (export "qjsd_result_ptr") (result i32) i32.const {ptr})
            (func (export "qjsd_result_len") (result i32) i32.const {length})
            (func (export "qjsd_error_ptr") (result i32) i32.const {ptr})
            (func (export "qjsd_error_len") (result i32) i32.const {length})
        )""",
        )
        artifacts.append((engine, module))
        monkeypatch.setattr(_diff, "_artifact", lambda: (engine, module))

    yield install
    for engine, module in artifacts:
        module.close()
        engine.close()


@pytest.mark.parametrize("alloc", [0, -1, 65536, 65535])
def test_host_rejects_bad_allocation_ranges(hostile_guest: Any, alloc: int) -> None:
    hostile_guest(alloc=alloc)
    with pytest.raises(SnapshotDiffError):
        snapshot_diff(b"ab", b"cd")


@pytest.mark.parametrize("status", [0, 7])
@pytest.mark.parametrize(("ptr", "length"), [(0, 1), (-1, 1), (65535, 2), (1024, -1)])
def test_host_rejects_bad_result_or_error_ranges(
    hostile_guest: Any,
    status: int,
    ptr: int,
    length: int,
) -> None:
    hostile_guest(ptr=ptr, length=length, status=status)
    with pytest.raises(SnapshotDiffError):
        snapshot_diff(b"a", b"b", max_bytes=1)


def test_host_checks_output_cap_before_read(hostile_guest: Any) -> None:
    hostile_guest(length=4099)
    with pytest.raises(SnapshotDiffError, match="result exceeds"):
        snapshot_diff(b"a", b"b", max_bytes=1)


def test_host_reads_nonzero_status_error_without_enum(hostile_guest: Any) -> None:
    hostile_guest(ptr=1024, length=1, status=123)
    with pytest.raises(SnapshotDiffError, match="b"):
        snapshot_diff(b"a", b"b")


def test_zero_length_input_and_result_skip_memory_copy(hostile_guest: Any) -> None:
    hostile_guest(ptr=0, length=0)
    assert snapshot_diff(b"", b"") == b""


def test_upper_option_bounds_are_accepted(hostile_guest: Any) -> None:
    hostile_guest(ptr=0, length=0)
    assert (
        snapshot_diff(
            b"",
            b"",
            max_bytes=256 * 1024 * 1024,
            memory_limit=1 << 32,
            fuel=(1 << 64) - 1,
        )
        == b""
    )


def test_runtime_trap_is_mapped_and_next_store_recovers(hostile_guest: Any) -> None:
    hostile_guest(body="unreachable")
    with pytest.raises(SnapshotDiffError) as error:
        snapshot_diff(b"a", b"b")
    assert isinstance(error.value.__cause__, wasmtime.Trap)
    hostile_guest(ptr=0, length=0)
    assert snapshot_diff(b"a", b"b") == b""


def test_host_applies_memory_and_fuel_limits(hostile_guest: Any) -> None:
    hostile_guest(ptr=0, length=0)
    with pytest.raises(SnapshotDiffError) as error:
        snapshot_diff(b"", b"", memory_limit=1)
    assert isinstance(error.value.__cause__, wasmtime.WasmtimeError)
    hostile_guest(body="(loop br 0) unreachable")
    with pytest.raises(SnapshotDiffError) as error:
        snapshot_diff(b"", b"", fuel=100)
    assert isinstance(error.value.__cause__, wasmtime.Trap)
    hostile_guest(ptr=0, length=0)
    assert snapshot_diff(b"", b"") == b""
