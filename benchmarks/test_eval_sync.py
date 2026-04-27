"""Sync eval benchmarks. See benchmarks/README.md.

Measure JS execution overhead for representative workloads, isolating
quickjs-rs's cost from the cost of the JS computation itself.
Setup (runtime, context, code strings) happens outside the
``benchmark(...)`` call.
"""

from __future__ import annotations

from pytest_codspeed import BenchmarkFixture

from quickjs_rs import Context


def bench_eval_noop(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """ctx.eval('undefined') — minimum round-trip through the eval
    pipeline. Measures the floor for sync eval overhead."""
    benchmark(ctx.eval, "undefined")


def bench_eval_arithmetic(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Simplest value-producing eval."""
    benchmark(ctx.eval, "1 + 2")


def bench_eval_string_concat(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """String allocation + return round-trip."""
    benchmark(ctx.eval, "'hello' + ' ' + 'world'")


def bench_eval_json_parse(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Parse a ~1KB JSON string in JS and return as Python dict."""
    # Build a JSON string of roughly 1KB. Deterministic content so
    # runs are comparable across invocations.
    payload = (
        '{"key": "value", "nums": [1,2,3,4,5,6,7,8,9,10], '
        '"nested": {"a": true, "b": false, "c": null}, '
        '"long_string": "' + ("x" * 800) + '"}'
    )
    code = f"JSON.parse({payload!r})"
    benchmark(ctx.eval, code)


def bench_eval_json_parse_10kb(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Parse a ~10KB JSON string — exercises the msgpack scratch
    buffer (64 KB starting cap, but the 10KB output tests the
    not-grown path; 100KB+ tests would exercise realloc)."""
    entries = ", ".join(f'"k{i}": {i}' for i in range(500))
    payload = "{" + entries + "}"
    code = f"JSON.parse({payload!r})"
    benchmark(ctx.eval, code)


def bench_eval_fibonacci_30(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """fib(30) recursive — pure-compute JS, no host calls, no GC
    pressure. Measures QuickJS interpreter speed on the canonical
    micro-benchmark."""
    code = """
        (() => {
            function fib(n) { return n < 2 ? n : fib(n-1) + fib(n-2); }
            return fib(30);
        })()
    """
    benchmark(ctx.eval, code)


def bench_eval_loop_1m(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """One million iterations of an empty loop — pure bytecode
    dispatch overhead, no allocation, no function calls."""
    code = "(() => { for (let i = 0; i < 1000000; i++) {} return 0; })()"
    benchmark(ctx.eval, code)


def bench_eval_regex(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Regex match on a 1KB string — measures JS built-in perf
    (libregexp in quickjs-ng).

    Wrapped in an IIFE so const declarations don't leak into the
    realm across iterations. pytest-codspeed runs the benchmark body
    many times on the same Context; bare top-level const would
    redeclare on the second iteration and throw SyntaxError.
    """
    code = """
        (() => {
            const s = 'a'.repeat(1000) + 'needle' + 'b'.repeat(500);
            return /needle/.test(s);
        })()
    """
    benchmark(ctx.eval, code)


def bench_eval_object_create_1k(benchmark: BenchmarkFixture, ctx: Context) -> None:
    """Create 1000 objects with 5 properties each — GC pressure.
    The objects go out of scope at the end of the eval so a full
    GC cycle is part of the measurement."""
    code = """
        (() => {
            const arr = [];
            for (let i = 0; i < 1000; i++) {
                arr.push({ a: i, b: i * 2, c: i * 3, d: i + 1, e: i - 1 });
            }
            return arr.length;
        })()
    """
    benchmark(ctx.eval, code)
