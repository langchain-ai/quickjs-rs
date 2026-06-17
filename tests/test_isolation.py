"""Context isolation under interleaved single-threaded load.

quickjs-rs is single-threaded *per Runtime*: a Runtime and its Contexts must be
created and used from one thread (they are ``!Send`` at the core, and the
underlying wasmtime-py bindings are explicitly not thread-safe — see
bytecodealliance/wasmtime-py#254). So this test does NOT spawn threads. Instead
it stands up many Runtimes × many Contexts and steps them in an interleaved
round-robin from a single thread, asserting no cross-context value bleed appears.

Each context keeps a private owner token + turn counter; every turn re-checks
both in JS and in Python, and the per-context host callback closes over its own
token. Any leakage between the many live, interleaved contexts surfaces as an
owner/turn mismatch.
"""

from __future__ import annotations

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
