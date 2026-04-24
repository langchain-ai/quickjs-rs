"""Marshaling benchmarks. See benchmarks/README.md.

Isolate the cost of Python ↔ JS value conversion from eval
overhead. Each benchmark does a full round-trip: set via globals
(Python → JS), read via eval (JS → Python). The measured delta
between similar-shaped benchmarks at different sizes indicates
the per-element cost of a given type.
"""

from __future__ import annotations

from pytest_codspeed import BenchmarkFixture

from quickjs_rs import Context


def bench_marshal_int(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Set and read a single integer via globals. Floor for the
    round-trip path."""

    def round_trip() -> object:
        ctx.globals["x"] = 42
        return ctx.eval("x")

    benchmark(round_trip)


def bench_marshal_string_1kb(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Round-trip a 1KB string. Small enough that scratch buffer
    growth isn't triggered."""
    payload = "x" * 1024

    def round_trip() -> object:
        ctx.globals["s"] = payload
        return ctx.eval("s")

    benchmark(round_trip)


def bench_marshal_string_100kb(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Round-trip a 100KB string — scratch buffer grows past the
    64KB initial cap, forcing at least one realloc."""
    payload = "x" * (100 * 1024)

    def round_trip() -> object:
        ctx.globals["s"] = payload
        return ctx.eval("s")

    benchmark(round_trip)


def bench_marshal_dict_flat_100(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Flat dict with 100 string keys — exercises the map encode/
    decode paths at a size past the fixmap boundary (15 entries)."""
    data = {f"key_{i}": f"value_{i}" for i in range(100)}

    def round_trip() -> object:
        ctx.globals["d"] = data
        return ctx.eval("d")

    benchmark(round_trip)


def bench_marshal_dict_nested_5(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """5-level nested dict — measures recursive encoder/decoder
    overhead separate from width."""
    data: dict[str, object] = {"leaf": 1}
    for _ in range(4):
        data = {"nested": data}

    def round_trip() -> object:
        ctx.globals["d"] = data
        return ctx.eval("d")

    benchmark(round_trip)


def bench_marshal_list_10k_ints(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Round-trip a 10,000-element list of integers. Exercises the
    array32 header path plus per-element float64 encoding (numbers
    are always float64 on the wire)."""
    data = list(range(10_000))

    def round_trip() -> object:
        ctx.globals["a"] = data
        return ctx.eval("a")

    benchmark(round_trip)


def bench_marshal_bytes_1mb(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Round-trip 1MB of bytes — Uint8Array path. Exercises the bin32
    encoding plus the scratch buffer's realloc growth beyond the
    initial 64KB cap."""
    payload = b"\x00" * (1024 * 1024)

    def round_trip() -> object:
        ctx.globals["b"] = payload
        return ctx.eval("b")

    benchmark(round_trip)
