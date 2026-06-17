"""Fleet benchmarks: standing up and running many runtimes/contexts.

quickjs-rs is single-threaded per Runtime (the wasmtime-py bindings are not
thread-safe — bytecodealliance/wasmtime-py#254), so "a bunch of runtimes at the
same time" means many live concurrently and stepped interleaved from ONE thread,
not run in parallel. Two separate signals:

  * ``bench_fleet_construct`` — cost of building the fleet (Runtime + Context +
    host-fn registration + global seeding) at scale, then tearing it down. This
    is the path the instance-leak / address-space bugs lived on.
  * ``bench_fleet_interleaved_tps`` — steady-state throughput driving an
    already-built fleet round-robin, so every context is repeatedly re-entered
    while its peers are live. Construction is excluded from the measured region.

Isolation *correctness* under the same interleaving is asserted separately in
tests/test_isolation.py; here we only measure time.

Local scaling via env: ``QJS_FLEET_RUNTIMES``, ``QJS_FLEET_CONTEXTS_PER_RUNTIME``,
``QJS_FLEET_TURNS``.
"""

from __future__ import annotations

import os

from pytest_codspeed import BenchmarkFixture

from quickjs_rs import Context, Runtime

# Modest defaults so the suite stays fast under codspeed instrumentation;
# override via env for heavier local runs.
RUNTIME_COUNT = 6
CONTEXTS_PER_RUNTIME = 4
TURNS = 20


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _turn_source(token: str) -> str:
    """JS for one turn: a little matrix work, a mutable-global write, and a
    host call — representative per-turn work, not a microbenchmark of any one
    op. Mirrors the workload asserted in tests/test_isolation.py."""
    return (
        """
        (() => {
            const OWNER = """
        + repr(token)
        + """;
            globalThis.__turn += 1;
            const turn = globalThis.__turn;
            let sum = 0;
            for (let i = 0; i < 16; i++) {
                for (let j = 0; j < 8; j++) sum += (i * 17 + j + turn) % 97;
            }
            globalThis.__shared_slot = OWNER + ":" + String(turn);
            const host = host_mix(sum);
            globalThis.__digest = (globalThis.__digest + sum + host.mix) % 2147483647;
            return JSON.parse(JSON.stringify({ owner: OWNER, turn, digest: globalThis.__digest }));
        })()
        """
    )


def _build_fleet(
    runtime_count: int, contexts_per_runtime: int
) -> tuple[list[Runtime], list[tuple[Context, str]]]:
    """Stand up the fleet: runtime_count Runtimes, each with
    contexts_per_runtime Contexts, every context seeded with its owner globals
    and a per-context host callback. Returns (runtimes, [(ctx, token), ...])."""
    runtimes: list[Runtime] = []
    contexts: list[tuple[Context, str]] = []
    for runtime_idx in range(runtime_count):
        rt = Runtime()
        runtimes.append(rt)
        for context_idx in range(contexts_per_runtime):
            token = f"runtime={runtime_idx};context={context_idx}"
            ctx = rt.new_context(timeout=5.0)
            ctx.eval(
                "globalThis.__owner = "
                f"{token!r}; globalThis.__turn = 0; globalThis.__digest = 0;"
            )

            @ctx.function(name="host_mix")
            def host_mix(n: int, _token: str = token) -> dict[str, int | str]:
                mixed = ((n * 1315423911) ^ 0x9E3779B9) & 0x7FFFFFFF
                return {"owner": _token, "mix": mixed}

            contexts.append((ctx, token))
    return runtimes, contexts


def _teardown(runtimes: list[Runtime], contexts: list[tuple[Context, str]]) -> None:
    for ctx, _ in contexts:
        ctx.close()
    for rt in runtimes:
        rt.close()


def bench_fleet_construct(benchmark: BenchmarkFixture) -> None:
    """Build a fleet of RUNTIME_COUNT × CONTEXTS_PER_RUNTIME contexts (with
    host-fn registration + global seeding) and tear it down. Measures
    creation-at-scale — the path the instance-leak / address-space issues
    lived on."""
    runtime_count = _env_int("QJS_FLEET_RUNTIMES", RUNTIME_COUNT)
    contexts_per_runtime = _env_int("QJS_FLEET_CONTEXTS_PER_RUNTIME", CONTEXTS_PER_RUNTIME)
    built: list[tuple[list[Runtime], list[tuple[Context, str]]]] = []

    def construct() -> None:
        built.append(_build_fleet(runtime_count, contexts_per_runtime))

    try:
        benchmark(construct)
    finally:
        for runtimes, contexts in built:
            _teardown(runtimes, contexts)


def bench_fleet_interleaved_tps(benchmark: BenchmarkFixture) -> None:
    """Drive an already-built fleet round-robin for TURNS turns: each turn
    steps EVERY live context once, so all contexts are continuously
    interleaved. Construction is outside the measured region — this isolates
    steady-state interleaved throughput."""
    runtime_count = _env_int("QJS_FLEET_RUNTIMES", RUNTIME_COUNT)
    contexts_per_runtime = _env_int("QJS_FLEET_CONTEXTS_PER_RUNTIME", CONTEXTS_PER_RUNTIME)
    turns = _env_int("QJS_FLEET_TURNS", TURNS)
    runtimes, contexts = _build_fleet(runtime_count, contexts_per_runtime)
    # Precompute per-context JS once so the measured loop is pure eval, not
    # Python string building.
    work = [(ctx, _turn_source(token)) for ctx, token in contexts]

    def run_round_robin() -> None:
        for _ in range(turns):
            for ctx, src in work:
                ctx.eval(src)

    try:
        benchmark(run_round_robin)
    finally:
        _teardown(runtimes, contexts)
