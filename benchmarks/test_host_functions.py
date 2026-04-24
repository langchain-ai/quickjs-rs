"""Host function call overhead benchmarks. See benchmarks/README.md.

Measure the per-call cost of crossing the JS → Python → JS boundary.
Sync path traffics through host_call; async path goes through
host_call_async + driving loop.
"""

from __future__ import annotations

import pytest
from pytest_codspeed import BenchmarkFixture

from quickjs_rs import Context


def bench_host_call_noop(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Host fn returns None — pure dispatch overhead, no marshal cost
    beyond the empty-arg/None-return minimum."""

    @ctx.function
    def noop() -> None:
        return None

    benchmark(ctx.eval, "noop()")


def bench_host_call_identity_int(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Host fn returns its integer argument — minimal marshal both
    ways (one int in, one int out)."""

    @ctx.function
    def ident(n: int) -> int:
        return n

    benchmark(ctx.eval, "ident(42)")


def bench_host_call_identity_dict(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Host fn returns a small dict — structured marshal cost on the
    return path."""

    @ctx.function
    def make_dict() -> dict[str, int]:
        return {"a": 1, "b": 2, "c": 3}

    benchmark(ctx.eval, "make_dict()")


def bench_host_call_100x_loop(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """JS loop calling a host fn 100 times — amortized per-call cost
    over a short JS body. Isolates per-host-call overhead from
    eval fixed costs."""

    @ctx.function
    def inc(n: int) -> int:
        return n + 1

    code = """
        (() => {
            let x = 0;
            for (let i = 0; i < 100; i++) x = inc(x);
            return x;
        })()
    """
    benchmark(ctx.eval, code)


@pytest.mark.benchmark
async def bench_host_call_async_noop(async_ctx: Context) -> None:
    """Async host fn that returns immediately — async dispatch
    overhead vs sync. Contrasts with bench_host_call_noop to
    quantify the eval_async pipeline's fixed cost on top of a
    host call with the same no-op semantic.

    Uses @pytest.mark.benchmark (whole-test timing) rather than
    the benchmark fixture because the benchmark body itself is
    async. The async_ctx fixture from conftest pre-registers an
    immediate async host fn named `instant`.
    """
    await async_ctx.eval_async("await instant(0)")
