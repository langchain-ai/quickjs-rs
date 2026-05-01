"""Snapshot name-extraction overhead benchmarks.

These cases provide short/medium/large pairs:
- "no_names" baseline with no top-level declarations
- "with_names" variant with many top-level ``var`` declarations

Compare each size pair to estimate extraction + registry-merge overhead.
These are intentionally narrower than ``test_snapshot.py``, which
benchmarks end-to-end snapshot create/restore costs.
"""

from __future__ import annotations

from pytest_codspeed import BenchmarkFixture

from quickjs_rs import Context

_SHORT_BYTES = 256
_MEDIUM_BYTES = 4 * 1024
_LARGE_BYTES = 96 * 1024


def _script_without_top_level_names(min_bytes: int) -> str:
    unit = "0;\n"
    repeats = (min_bytes // len(unit)) + 1
    return unit * repeats


def _script_with_top_level_var_decls(min_bytes: int) -> str:
    lines: list[str] = []
    total = 0
    i = 0
    while total < min_bytes:
        line = f"var n{i};\n"
        lines.append(line)
        total += len(line)
        i += 1
    lines.append("0;\n")
    return "".join(lines)


_SHORT_NO_NAMES = _script_without_top_level_names(_SHORT_BYTES)
_SHORT_WITH_NAMES = _script_with_top_level_var_decls(_SHORT_BYTES)
_MEDIUM_NO_NAMES = _script_without_top_level_names(_MEDIUM_BYTES)
_MEDIUM_WITH_NAMES = _script_with_top_level_var_decls(_MEDIUM_BYTES)
_LARGE_NO_NAMES = _script_without_top_level_names(_LARGE_BYTES)
_LARGE_WITH_NAMES = _script_with_top_level_var_decls(_LARGE_BYTES)


def bench_snapshot_extraction_short_no_names(benchmark: BenchmarkFixture, ctx: Context) -> None:
    benchmark(ctx.eval, _SHORT_NO_NAMES)


def bench_snapshot_extraction_short_with_names(
    benchmark: BenchmarkFixture,
    ctx: Context,
) -> None:
    benchmark(ctx.eval, _SHORT_WITH_NAMES)


def bench_snapshot_extraction_medium_no_names(
    benchmark: BenchmarkFixture,
    ctx: Context,
) -> None:
    benchmark(ctx.eval, _MEDIUM_NO_NAMES)


def bench_snapshot_extraction_medium_with_names(
    benchmark: BenchmarkFixture,
    ctx: Context,
) -> None:
    benchmark(ctx.eval, _MEDIUM_WITH_NAMES)


def bench_snapshot_extraction_large_no_names(benchmark: BenchmarkFixture, ctx: Context) -> None:
    benchmark(ctx.eval, _LARGE_NO_NAMES)


def bench_snapshot_extraction_large_with_names(
    benchmark: BenchmarkFixture,
    ctx: Context,
) -> None:
    benchmark(ctx.eval, _LARGE_WITH_NAMES)
