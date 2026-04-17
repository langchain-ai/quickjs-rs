# CLAUDE.md

Instructions for Claude Code working in this repository. Read this file in full at the start of every session.

## Project

`quickjs-rs` (formerly `quickjs-wasm`) is a Python library for executing JavaScript from a Python host, using PyO3 + rquickjs (Rust bindings to QuickJS). Two layers:

1. `quickjs_rs._engine` — PyO3 Rust extension wrapping rquickjs. Single compiled `.so`.
2. `quickjs_rs` — user-facing Python API (`Runtime`, `Context`, `Handle`).

This repo previously used a wasm-based architecture (wasmtime + C shim). v0.3 rewrites the bridge layer to PyO3 + rquickjs, dropping the wasm layer entirely. The Python API is unchanged. The git history from the wasm era (v0.1, v0.2) is preserved and useful for understanding design decisions — the spec evolution story is traceable through commit bodies.

## Authoritative docs

- `spec/implementation.md` — the complete rewrite spec. Section numbers (§3, §6, etc.) are referenced throughout.
- `spec/benchmarks.md` — benchmark cases and targets.
- `spec/quickjs.wit` — archival; documents the original wasm interface contract. Not authoritative for this repo, but useful for understanding the API design rationale.

**The spec and the code never disagree.** If you need behavior that contradicts the spec, update the spec in the same commit that changes the code.

## Implementation order

Follow §15 of `spec/implementation.md`. Phase 0 is the transition scaffolding (rename, remove wasm, add maturin). Phases 1-3 are the reimplementation, assertion-by-assertion, same rhythm as v0.1 and v0.2.

If you're in a session during phase 0, the goal is clean intermediate commits that each leave the repo in a consistent state — even if tests don't pass yet (the bridge is being swapped). Each phase 0 commit should be self-contained: "rename done, everything else unchanged," then "wasm removed, nothing added yet," then "maturin builds, extension loads empty."

If you're in a session during phases 1-3, the goal is the same as v0.1/v0.2: one assertion green per commit, north star is the acceptance test.

## Build commands

```bash
# Development build
maturin develop --release

# With dev extras
maturin develop --release -E dev

# Run tests
pytest

# Run benchmarks
pytest benchmarks/ --codspeed

# Build release wheel
maturin build --release

# Type check and lint
mypy quickjs_rs
ruff check
```

No submodule init. No WASI-SDK. No separate wasm build step. `maturin develop` handles everything.

## Non-negotiable invariants

From §6 (Rust extension):
- All QjsHandle methods validate the handle is not disposed; InvalidHandleError on use-after-dispose
- Handles are bound to their creating context; cross-context use raises InvalidHandleError
- rquickjs manages JSValue refcounting via Rust ownership — no manual JS_DupValue / JS_FreeValue anywhere in our code
- The interrupt handler acquires the GIL to call into Python; verify no deadlock on reentrant GIL acquisition from the eval thread
- Host function dispatch uses fn_id + Python-side registry, same as predecessor. Rust side doesn't store Python callables directly.

From §7 (Python API — unchanged from predecessor):
- Every public method returns successfully or raises a QuickJSError subclass
- Handle.__del__ emits ResourceWarning if not disposed
- Context manager semantics: Runtime.__exit__ closes all contexts, Context.__exit__ disposes all handles

From §7.4 (async — unchanged from predecessor):
- Only one eval_async in flight per context. Second raises ConcurrentEvalError.
- Sync eval raises ConcurrentEvalError if an async host call fires during execution.
- Async host-function detection is inspect.iscoroutinefunction (auto). ctx.register(..., is_async=True/False) is the explicit override.
- eval_async timeout is cumulative across calls on the same context. timeout= kwarg overrides per-call.
- Cancellation: catch CancelledError at the driving loop's await, cancel the internal TaskGroup, reject in-flight promises with HostCancellationError, run pending jobs one final time, re-raise unless JS absorbed it.

## Commit discipline

**Linear history.** No merge commits on main.

```bash
git config pull.rebase true
git config merge.ff only
```

**One commit per meaningful unit of progress.** The natural unit is "a new assertion in the acceptance test turns green" or "a self-contained section of the spec is fully implemented."

**Commit message format.** Subject with scope prefix, imperative mood. Body explains why — the decision, the spec section, the test assertion that turns green.

```
engine: implement eval with primitive marshaling

Implements §6.3 eval and §6.6 marshaling for primitives (null, bool,
number, string). rquickjs FromJs/IntoJs traits handle the JS↔Rust
conversion; PyO3 handles Rust↔Python.

Turns green: ctx.eval("1 + 2") == 3, ctx.eval("'hello'") == "hello".
Refs: spec/implementation.md §6.3, §6.6.
```

Scope prefixes: `engine:` (Rust/PyO3), `api:` (Python layer), `tests:`, `bench:`, `spec:`, `build:`, `ci:`, `docs:`.

**Spec changes travel with code changes.** Same commit when the code change prompted the spec clarification.

## Before every commit

1. `pytest` passes — no test that was green goes red
2. `mypy quickjs_rs` is clean
3. `ruff check` is clean
4. If Rust code changed, `maturin develop --release` was run and the extension is current
5. If the spec changed, it's included in this commit

## What not to do

- Do not add runtime dependencies. The wheel is self-contained. Zero deps is a feature.
- Do not reintroduce wasm, wasmtime, or WASI. This repo deliberately removed that layer in v0.3's phase 0. If someone needs the wasm sandbox, `quickjs-wasm` v0.2 is frozen on PyPI.
- Do not reintroduce the C shim, _bridge.py, or _msgpack.py. These were removed in phase 0 and replaced by the Rust extension.
- Do not write raw C or call QuickJS's C API directly. Use rquickjs's safe Rust API. If rquickjs doesn't expose something you need, open an issue on rquickjs or use its `unsafe` escape hatch with a comment explaining why.
- Do not use `unsafe` Rust without a comment explaining the safety invariant.
- Do not store Python callables (PyObject) in rquickjs data structures. Use the fn_id dispatch pattern per §6.5.
- Do not change the public Python API without updating the spec.
- Do not commit code with failing tests (except during phase 0 where tests fail by design — the bridge is being swapped).

## Spec-conformance tripwires

When implementing a design choice that isn't obvious from reading the code — cancellation absorption, budget independence, structural detection — add a single test whose docstring states the choice and references the spec section. The test exists to fail red if a future refactor silently reverts the choice.

Existing tripwire tests transfer from the predecessor repo. Preserve their docstrings and spec references (updated to the new spec section numbers if they shift).

## Integration tests are load-bearing

test_smoke.py's acceptance tests caught real bugs in the predecessor that focused tests missed. Do not skip or defer integration tests on the grounds that "focused tests already cover this."

## Benchmarks

Performance benchmarks live in `benchmarks/`, separate from tests. See `spec/benchmarks.md`.

The rewrite's first benchmark run is the most important one — it validates the architectural thesis (§11 targets). If `bench_runtime_create` doesn't drop to single-digit milliseconds, investigate before committing.

Run `pytest benchmarks/ --codspeed` after any change to the Rust extension.

## When in doubt

If the spec is ambiguous, stop and ask rather than guessing. Ambiguity in a spec is a bug in the spec.

If rquickjs doesn't expose something the spec requires, check rquickjs's GitHub issues and docs. If it's genuinely missing, flag it — we may need to contribute upstream or use a lower-level API.

## File map

```
spec/implementation.md      The rewrite spec (authoritative)
spec/benchmarks.md          Benchmark cases and targets
spec/quickjs.wit            Archival interface contract
Cargo.toml                  Rust dependencies (rquickjs, PyO3)
src/lib.rs                  §6 — the PyO3 extension
quickjs_rs/runtime.py       §7 — Runtime
quickjs_rs/context.py       §7 — Context + eval_async
quickjs_rs/handle.py        §7 — Handle
quickjs_rs/globals.py       §7 — Globals proxy
quickjs_rs/errors.py        §9 — Exception hierarchy
tests/test_smoke.py         §13 — Acceptance tests
tests/test_*.py             Focused tests (transferred)
benchmarks/                 Benchmark suite (transferred)
```