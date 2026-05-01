"""End-to-end snapshot creation and restore benchmarks.

These cases measure the public Snapshot V1 workflow directly:
- ``Context.create_snapshot()`` on an already-populated context
- ``Runtime.restore_snapshot()`` into an existing destination context

They complement ``test_snapshot_extraction.py``, which isolates the
AST name-extraction + registry-merge cost that now sits on the eval
path. This file is intended to answer the broader question: how much
does snapshotting itself cost once the tracked state already exists?
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from pytest_codspeed import BenchmarkFixture

from quickjs_rs import Context, Runtime, Snapshot


@dataclass(frozen=True)
class _SnapshotShape:
    """Deterministic benchmark shape for snapshot payload generation."""

    top_level_count: int
    array_len: int


@dataclass
class _SnapshotBenchCase:
    """Prepared source/restore contexts for a single benchmark size."""

    source_ctx: Context
    restore_ctx: Context
    snapshot: Snapshot


_SMALL_SHAPE = _SnapshotShape(top_level_count=8, array_len=4)
_MEDIUM_SHAPE = _SnapshotShape(top_level_count=48, array_len=8)
_LARGE_SHAPE = _SnapshotShape(top_level_count=192, array_len=16)


def _build_snapshot_source(shape: _SnapshotShape) -> str:
    """Build deterministic top-level state with aliasing and nested data."""
    lines = [
        "const shared = {",
        "  stamp: 'shared',",
        "  meta: { version: 1, stable: true },",
        f"  seed: [{', '.join(str(i) for i in range(shape.array_len))}],",
        "};",
    ]
    for i in range(shape.top_level_count):
        values = ", ".join(str((i * 17 + j) % 101) for j in range(shape.array_len))
        even = "true" if i % 2 == 0 else "false"
        lines.extend(
            [
                f"const item{i} = {{",
                f"  id: {i},",
                f"  name: 'item{i}',",
                f"  values: [{values}],",
                "  shared,",
                "  nested: {",
                f"    even: {even},",
                f"    weight: {(i * 3) % 29},",
                f"    next: {(i + 1) % shape.top_level_count},",
                "  },",
                "};",
            ]
        )
    lines.append("const summary = { count: " + str(shape.top_level_count) + ", shared };")
    return "\n".join(lines)


def _make_snapshot_case(rt: Runtime, shape: _SnapshotShape) -> _SnapshotBenchCase:
    """Prepare a source context, destination context, and baseline snapshot."""
    source_ctx = rt.new_context()
    restore_ctx = rt.new_context()
    source_ctx.eval(_build_snapshot_source(shape))
    return _SnapshotBenchCase(
        source_ctx=source_ctx,
        restore_ctx=restore_ctx,
        snapshot=source_ctx.create_snapshot(),
    )


@pytest.fixture
def snapshot_case_small(rt: Runtime) -> Iterator[_SnapshotBenchCase]:
    """Small snapshot payload: a handful of names and short arrays."""
    case = _make_snapshot_case(rt, _SMALL_SHAPE)
    try:
        yield case
    finally:
        case.source_ctx.close()
        case.restore_ctx.close()


@pytest.fixture
def snapshot_case_medium(rt: Runtime) -> Iterator[_SnapshotBenchCase]:
    """Medium snapshot payload: dozens of names and moderate nesting."""
    case = _make_snapshot_case(rt, _MEDIUM_SHAPE)
    try:
        yield case
    finally:
        case.source_ctx.close()
        case.restore_ctx.close()


@pytest.fixture
def snapshot_case_large(rt: Runtime) -> Iterator[_SnapshotBenchCase]:
    """Large snapshot payload: hundreds of names and wider arrays."""
    case = _make_snapshot_case(rt, _LARGE_SHAPE)
    try:
        yield case
    finally:
        case.source_ctx.close()
        case.restore_ctx.close()


def bench_snapshot_create_small(
    benchmark: BenchmarkFixture,
    snapshot_case_small: _SnapshotBenchCase,
) -> None:
    """Snapshot a small pre-populated context."""
    benchmark(snapshot_case_small.source_ctx.create_snapshot)


def bench_snapshot_create_medium(
    benchmark: BenchmarkFixture,
    snapshot_case_medium: _SnapshotBenchCase,
) -> None:
    """Snapshot a medium pre-populated context."""
    benchmark(snapshot_case_medium.source_ctx.create_snapshot)


def bench_snapshot_create_large(
    benchmark: BenchmarkFixture,
    snapshot_case_large: _SnapshotBenchCase,
) -> None:
    """Snapshot a large pre-populated context."""
    benchmark(snapshot_case_large.source_ctx.create_snapshot)


def bench_snapshot_restore_small(
    benchmark: BenchmarkFixture,
    rt: Runtime,
    snapshot_case_small: _SnapshotBenchCase,
) -> None:
    """Restore a small snapshot into an existing destination context."""
    benchmark(rt.restore_snapshot, snapshot_case_small.snapshot, snapshot_case_small.restore_ctx)


def bench_snapshot_restore_medium(
    benchmark: BenchmarkFixture,
    rt: Runtime,
    snapshot_case_medium: _SnapshotBenchCase,
) -> None:
    """Restore a medium snapshot into an existing destination context."""
    benchmark(rt.restore_snapshot, snapshot_case_medium.snapshot, snapshot_case_medium.restore_ctx)


def bench_snapshot_restore_large(
    benchmark: BenchmarkFixture,
    rt: Runtime,
    snapshot_case_large: _SnapshotBenchCase,
) -> None:
    """Restore a large snapshot into an existing destination context."""
    benchmark(rt.restore_snapshot, snapshot_case_large.snapshot, snapshot_case_large.restore_ctx)
