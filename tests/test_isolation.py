"""Context isolation under load — interleaved single-thread and concurrent.

A Runtime and its Contexts are ``!Send`` (use one Runtime from one thread), but
DIFFERENT Runtimes may be created and driven concurrently from different OS
threads. The host-function imports are registered once on a process-shared
Linker and routed to the calling instance via a thread-local, so concurrent
context create/eval/close across threads neither corrupts wasmtime-py's
process-global host-function table nor misroutes a host call to the wrong
instance (see ``_engine._CUR`` / the shared-Linker note).

Both tests give each context a private owner token + turn counter, re-checked in
JS and in Python every turn, with the per-context host callback closing over its
own token. Any cross-context bleed — or a host call routed to the wrong instance
— surfaces as an owner/turn mismatch.

- ``test_interleaved_context_isolation``: many contexts on ONE thread, stepped
  round-robin (interleaving exposes shared/leaked state).
- ``test_threaded_context_isolation``: many threads, each owning its own
  Runtime+Contexts, running concurrently (exposes the global-table race and
  thread-local misrouting).
"""

from __future__ import annotations

import queue
import threading

from quickjs_rs import Context, Runtime

# Workload shape. Many runtimes, several contexts each, stepped round-robin so
# that every context is repeatedly re-entered while its peers are also live —
# the interleaving is what would expose shared/leaked state.
RUNTIME_COUNT = 6
CONTEXTS_PER_RUNTIME = 4
TURNS_PER_CONTEXT = 25


def _turn_source(token: str) -> str:
    """JS for one turn: re-verify owner, bump the turn counter, do a little
    work, exercise a mutable global slot + the host callback, return a
    JSON-round-tripped payload."""
    return (
        """
        (() => {
            const OWNER = """
        + repr(token)
        + """;
            // Context-local state must always retain its original owner.
            if (globalThis.__owner !== OWNER) {
                throw new Error("owner mismatch: " + String(globalThis.__owner));
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

            // A mutable global write/read surfaces cross-context contamination
            // if global slots were accidentally shared between instances.
            globalThis.__shared_slot = OWNER + ":" + String(turn);
            if (globalThis.__shared_slot !== OWNER + ":" + String(turn)) {
                throw new Error("shared slot mismatch");
            }

            const host = host_mix(sum);
            if (!host || host.owner !== OWNER) {
                throw new Error("host owner mismatch");
            }

            globalThis.__digest =
                (globalThis.__digest + sum + host.mix) % 2147483647;

            const payload = { owner: OWNER, turn, digest: globalThis.__digest, sum };
            // Force a plain-JSON crossing to exercise the conversion path.
            return JSON.parse(JSON.stringify(payload));
        })()
        """
    )


def test_interleaved_context_isolation() -> None:
    """Many interleaved contexts on one thread keep their state private."""
    runtimes: list[Runtime] = []
    # (ctx, token) for every live context, across all runtimes, stepped together.
    contexts: list[tuple[Context, str]] = []
    total_ops = 0
    try:
        for runtime_idx in range(RUNTIME_COUNT):
            rt = Runtime()
            runtimes.append(rt)
            for context_idx in range(CONTEXTS_PER_RUNTIME):
                token = f"runtime={runtime_idx};context={context_idx}"
                ctx = rt.new_context(timeout=5.0)
                ctx.eval(
                    "globalThis.__owner = "
                    f"{token!r}; "
                    "globalThis.__turn = 0; "
                    "globalThis.__digest = 0;"
                )

                # Host callback closes over THIS context's token; a mismatched
                # token on return would indicate context crossover in bridge
                # execution.
                @ctx.function(name="host_mix")
                def host_mix(n: int, _token: str = token) -> dict[str, int | str]:
                    mixed = ((n * 1315423911) ^ 0x9E3779B9) & 0x7FFFFFFF
                    return {"owner": _token, "mix": mixed}

                contexts.append((ctx, token))

        # Round-robin: every turn steps EVERY live context once, so all contexts
        # are continuously interleaved rather than run to completion one by one.
        for turn in range(1, TURNS_PER_CONTEXT + 1):
            for ctx, token in contexts:
                result = ctx.eval(_turn_source(token))
                assert isinstance(result, dict), f"unexpected result type: {type(result).__name__}"
                assert result.get("owner") == token, (
                    f"owner token mismatch: expected={token!r} got={result.get('owner')!r}"
                )
                assert result.get("turn") == turn, (
                    f"turn mismatch: expected={turn} got={result.get('turn')!r}"
                )
                total_ops += 1
    finally:
        for ctx, _ in contexts:
            ctx.close()
        for rt in runtimes:
            rt.close()

    assert total_ops == RUNTIME_COUNT * CONTEXTS_PER_RUNTIME * TURNS_PER_CONTEXT


# Threaded shape: enough threads and create/close churn to exercise the
# process-global host-function table under contention.
THREAD_COUNT = 8
THREAD_RUNTIMES = 2
THREAD_CONTEXTS = 3
THREAD_TURNS = 20


def test_threaded_context_isolation() -> None:
    """Concurrent threads, each owning its own Runtimes+Contexts, keep state
    private. This guards two thread-safety properties of the shared-Linker +
    thread-local design: (1) concurrent context create/eval/close does not
    corrupt wasmtime-py's process-global host-function table, and (2) a host
    call is always routed to the calling instance, never a peer on another
    thread (a misroute would show up as an owner-token mismatch)."""
    failures: queue.Queue[str] = queue.Queue()

    def worker(worker_idx: int) -> None:
        try:
            for runtime_idx in range(THREAD_RUNTIMES):
                with Runtime() as rt:
                    contexts: list[tuple[Context, str]] = []
                    for context_idx in range(THREAD_CONTEXTS):
                        token = f"w={worker_idx};rt={runtime_idx};ctx={context_idx}"
                        ctx = rt.new_context(timeout=5.0)
                        ctx.eval(
                            "globalThis.__owner = "
                            f"{token!r}; "
                            "globalThis.__turn = 0; "
                            "globalThis.__digest = 0;"
                        )

                        @ctx.function(name="host_mix")
                        def host_mix(n: int, _token: str = token) -> dict[str, int | str]:
                            mixed = ((n * 1315423911) ^ 0x9E3779B9) & 0x7FFFFFFF
                            return {"owner": _token, "mix": mixed}

                        contexts.append((ctx, token))

                    for turn in range(1, THREAD_TURNS + 1):
                        for ctx, token in contexts:
                            result = ctx.eval(_turn_source(token))
                            if not isinstance(result, dict):
                                raise AssertionError(f"bad result type: {type(result).__name__}")
                            if result.get("owner") != token:
                                raise AssertionError(
                                    f"owner mismatch: expected={token!r} "
                                    f"got={result.get('owner')!r}"
                                )
                            if result.get("turn") != turn:
                                raise AssertionError(
                                    f"turn mismatch: expected={turn} got={result.get('turn')!r}"
                                )
                    for ctx, _ in contexts:
                        ctx.close()
        except BaseException as exc:  # noqa: BLE001 - report any worker failure
            failures.put(f"worker={worker_idx}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREAD_COUNT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    collected = []
    while not failures.empty():
        collected.append(failures.get_nowait())
    assert not collected, "\n".join(collected)
