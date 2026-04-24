"""CodSpeed memory benchmarks for quickjs-rs.

These cases are intentionally small and deterministic so CodSpeed memory
mode can track allocation regressions commit-to-commit.
"""

from __future__ import annotations

from typing import Any

from pytest_codspeed import BenchmarkFixture

from quickjs_rs import Runtime

MB = 1024 * 1024


def _alloc_payload(ctx: Any, payload_bytes: int) -> None:
    ctx.eval(
        f"""
        (() => {{
            const payload = [];
            let remaining = {payload_bytes};
            const chunk = 256 * 1024;
            while (remaining > 0) {{
                const n = Math.min(chunk, remaining);
                payload.push(new Uint8Array(n));
                remaining -= n;
            }}
            globalThis.__memory_payload = payload;
            return payload.length;
        }})()
        """
    )


def _clear_payload(ctx: Any) -> None:
    ctx.eval("globalThis.__memory_payload = undefined")


def bench_mem_runtime_context_create(benchmark: BenchmarkFixture) -> None:
    """Allocation profile of creating + closing one runtime/context pair."""

    def run_once() -> None:
        rt = Runtime(memory_limit=64 * MB)
        ctx = rt.new_context()
        ctx.close()
        rt.close()

    benchmark(run_once)


def bench_mem_runtime_4x_context_payload(benchmark: BenchmarkFixture) -> None:
    """Allocation profile for moderate fan-out + payload pinning."""

    def run_once() -> None:
        rt = Runtime(memory_limit=64 * MB)
        ctxs = [rt.new_context() for _ in range(4)]
        try:
            for ctx in ctxs:
                _alloc_payload(ctx, payload_bytes=512 * 1024)
        finally:
            for ctx in ctxs:
                try:
                    ctx.close()
                except Exception:
                    pass
            rt.close()

    benchmark(run_once)


def bench_mem_gc_reclaim_cycle(benchmark: BenchmarkFixture) -> None:
    """Allocation profile for clear + explicit QuickJS GC."""

    def run_once() -> None:
        rt = Runtime(memory_limit=64 * MB)
        ctx = rt.new_context()
        try:
            _alloc_payload(ctx, payload_bytes=2 * MB)
            _clear_payload(ctx)
            rt.run_gc()
        finally:
            ctx.close()
            rt.close()

    benchmark(run_once)
