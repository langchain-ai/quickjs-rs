"""Threaded stress benchmark: isolation under concurrent load.

Measures throughput (operations per second) while asserting no
cross-context value contamination appears during the run.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from quickjs_rs import Runtime


@dataclass(frozen=True)
class ThreadedStressStats:
    """Summary metrics from a threaded runtime/context stress run."""

    thread_count: int
    runtimes_per_thread: int
    contexts_per_runtime: int
    turns_per_context: int
    total_operations: int
    elapsed_seconds: float
    tps: float
    failures: tuple[str, ...]


def run_threaded_runtime_context_stress(
    *,
    thread_count: int,
    runtimes_per_thread: int,
    contexts_per_runtime: int,
    turns_per_context: int,
) -> ThreadedStressStats:
    """Run a threaded stress workload and return stats.

    Safety objective:
    each context maintains a private owner token and turn counter; every
    turn checks both in JS and Python. Any cross-context value bleed
    should surface as owner/turn mismatch.
    """
    for name, value in (
        ("thread_count", thread_count),
        ("runtimes_per_thread", runtimes_per_thread),
        ("contexts_per_runtime", contexts_per_runtime),
        ("turns_per_context", turns_per_context),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be > 0, got {value}")

    # Workers report independent failures/throughput without sharing mutable state.
    failures_q: queue.Queue[str] = queue.Queue()
    ops_q: queue.Queue[int] = queue.Queue()

    def worker(worker_idx: int) -> None:
        local_ops = 0
        try:
            for runtime_idx in range(runtimes_per_thread):
                with Runtime() as rt:
                    contexts: list[tuple[object, str]] = []
                    try:
                        for context_idx in range(contexts_per_runtime):
                            # Identity tag for this exact (thread, runtime, context) slot.
                            token = (
                                f"worker={worker_idx};runtime={runtime_idx};context={context_idx}"
                            )
                            ctx = rt.new_context(timeout=5.0)
                            ctx.eval(
                                "globalThis.__owner = "
                                f"{token!r}; "
                                "globalThis.__turn = 0; "
                                "globalThis.__digest = 0;"
                            )

                            # Host callback closes over this context's token; mismatched token
                            # on return indicates context crossover in bridge execution.
                            @ctx.function(name="host_mix")
                            def host_mix(n: int, _token: str = token) -> dict[str, int | str]:
                                # special RNG generator
                                mixed = ((n * 1315423911) ^ 0x9E3779B9) & 0x7FFFFFFF
                                return {"owner": _token, "mix": mixed}

                            contexts.append((ctx, token))

                        for turn in range(1, turns_per_context + 1):
                            for raw_ctx, token in contexts:
                                ctx = raw_ctx
                                result = ctx.eval(
                                    """
                                    (() => {
                                        const OWNER = """
                                    + repr(token)
                                    + """;
                                        // Context-local state must always retain original owner.
                                        if (globalThis.__owner !== OWNER) {
                                            throw new Error(
                                                "owner mismatch: " +
                                                String(globalThis.__owner)
                                            );
                                        }
                                        globalThis.__turn += 1;
                                        const turn = globalThis.__turn;

                                        const matrix = [];
                                        for (let i = 0; i < 16; i++) {
                                            const row = [];
                                            for (let j = 0; j < 8; j++) {
                                                row.push((i * 17 + j + turn) % 97);
                                            }
                                            matrix.push(row);
                                        }
                                        let sum = 0;
                                        for (const row of matrix) {
                                            for (const v of row) sum += v;
                                        }

                                        // A mutable global write/read can surface cross-context
                                        // contamination if global slots are accidentally shared.
                                        globalThis.__shared_slot =
                                            OWNER + ":" + String(turn);
                                        if (
                                            globalThis.__shared_slot !==
                                            OWNER + ":" + String(turn)
                                        ) {
                                            throw new Error("shared slot mismatch");
                                        }

                                        const host = host_mix(sum);
                                        if (!host || host.owner !== OWNER) {
                                            throw new Error("host owner mismatch");
                                        }

                                        globalThis.__digest =
                                            (globalThis.__digest + sum + host.mix) %
                                            2147483647;

                                        const payload = {
                                            owner: OWNER,
                                            turn,
                                            digest: globalThis.__digest,
                                            sum,
                                        };
                                        // Force plain-JSON crossing to exercise conversion path.
                                        return JSON.parse(JSON.stringify(payload));
                                    })()
                                    """
                                )
                                if not isinstance(result, dict):
                                    raise AssertionError(
                                        f"unexpected result type: {type(result).__name__}"
                                    )
                                owner = result.get("owner")
                                got_turn = result.get("turn")
                                if owner != token:
                                    raise AssertionError(
                                        f"owner token mismatch: expected={token!r} got={owner!r}"
                                    )
                                if got_turn != turn:
                                    raise AssertionError(
                                        f"turn mismatch: expected={turn} got={got_turn!r}"
                                    )
                                local_ops += 1
                    finally:
                        # Explicit close keeps resource release deterministic under stress.
                        for raw_ctx, _ in contexts:
                            raw_ctx.close()
        except BaseException as exc:
            failures_q.put(f"worker={worker_idx} failed: {type(exc).__name__}: {exc}")
        finally:
            ops_q.put(local_ops)

    start = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    # Summarize counts centrally for deterministic post-run assertions.
    total_operations = 0
    while not ops_q.empty():
        total_operations += ops_q.get_nowait()

    failures: list[str] = []
    while not failures_q.empty():
        failures.append(failures_q.get_nowait())

    tps = total_operations / elapsed if elapsed > 0 else float("inf")
    return ThreadedStressStats(
        thread_count=thread_count,
        runtimes_per_thread=runtimes_per_thread,
        contexts_per_runtime=contexts_per_runtime,
        turns_per_context=turns_per_context,
        total_operations=total_operations,
        elapsed_seconds=elapsed,
        tps=tps,
        failures=tuple(failures),
    )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def bench_threaded_runtime_context_isolation_tps(benchmark: Any) -> None:
    """Run a threaded stress workload and report TPS.

    Env overrides for local scaling:
    - ``QJS_STRESS_THREADS``
    - ``QJS_STRESS_RUNTIMES_PER_THREAD``
    - ``QJS_STRESS_CONTEXTS_PER_RUNTIME``
    - ``QJS_STRESS_TURNS_PER_CONTEXT``
    """
    threads = _env_int("QJS_STRESS_THREADS", 12)
    runtimes_per_thread = _env_int("QJS_STRESS_RUNTIMES_PER_THREAD", 2)
    contexts_per_runtime = _env_int("QJS_STRESS_CONTEXTS_PER_RUNTIME", 4)
    turns_per_context = _env_int("QJS_STRESS_TURNS_PER_CONTEXT", 60)
    expected_ops = threads * runtimes_per_thread * contexts_per_runtime * turns_per_context

    latest = {"tps": 0.0, "ops": 0, "elapsed": 0.0}

    def run_once() -> None:
        stats = run_threaded_runtime_context_stress(
            thread_count=threads,
            runtimes_per_thread=runtimes_per_thread,
            contexts_per_runtime=contexts_per_runtime,
            turns_per_context=turns_per_context,
        )
        assert not stats.failures, "\n".join(stats.failures)
        assert stats.total_operations == expected_ops
        latest["tps"] = stats.tps
        latest["ops"] = stats.total_operations
        latest["elapsed"] = stats.elapsed_seconds

    benchmark(run_once)
    print(
        "threaded_stress_config="
        f"threads={threads},"
        f"runtimes_per_thread={runtimes_per_thread},"
        f"contexts_per_runtime={contexts_per_runtime},"
        f"turns_per_context={turns_per_context},"
        f"expected_ops={expected_ops}"
    )
    print(
        "threaded_stress_tps="
        f"{latest['tps']:.2f} ops/s "
        f"(ops={latest['ops']}, elapsed={latest['elapsed']:.3f}s)"
    )
