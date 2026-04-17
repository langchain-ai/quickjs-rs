"""Startup benchmarks. See spec/benchmarks.md §5.1.

These measure the fixed cost users pay before any JS runs. For agent
workloads that create a fresh context per tool invocation, startup is
on the critical path.

Each benchmark constructs its own Runtime explicitly — the module-
scoped ``rt`` fixture from conftest is NOT used here, because that
fixture amortizes the cost we're trying to measure.
"""

from __future__ import annotations

from pytest_codspeed import BenchmarkFixture

from quickjs_rs import Runtime


def bench_runtime_create(benchmark: BenchmarkFixture) -> None:
    """Runtime() — wasm module load, instantiation, memory limit
    configuration. Expected: 5–20 ms on a modern laptop."""
    rts: list[Runtime] = []

    def make() -> None:
        rts.append(Runtime())

    try:
        benchmark(make)
    finally:
        for r in rts:
            r.close()


def bench_context_create(benchmark: BenchmarkFixture) -> None:
    """rt.new_context() on an already-loaded Runtime. Expected:
    0.1–1 ms. The Runtime is created outside the measured region."""
    rt = Runtime()

    def create_ctx() -> None:
        ctx = rt.new_context()
        ctx.close()

    try:
        benchmark(create_ctx)
    finally:
        rt.close()


def bench_runtime_and_context(benchmark: BenchmarkFixture) -> None:
    """Runtime() + new_context() — full cold-start path. Expected:
    5–25 ms."""
    created: list[tuple[Runtime, object]] = []

    def cold_start() -> None:
        rt = Runtime()
        ctx = rt.new_context()
        created.append((rt, ctx))

    try:
        benchmark(cold_start)
    finally:
        for rt, ctx in created:
            ctx.close()  # type: ignore[attr-defined]
            rt.close()


def bench_context_create_10x(benchmark: BenchmarkFixture) -> None:
    """Ten contexts on one runtime — amortized context cost. Exposes
    per-context setup overhead that a single-context measurement
    can't distinguish from constant startup noise."""
    rt = Runtime()

    def create_10() -> None:
        ctxs = [rt.new_context() for _ in range(10)]
        for c in ctxs:
            c.close()

    try:
        benchmark(create_10)
    finally:
        rt.close()
