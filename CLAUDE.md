# CLAUDE.md

Instructions for Claude Code working in this repository. Read this file in full at the start of every session.

## Project

`quickjs-rs` (formerly `quickjs-wasm`) is a Python library for executing JavaScript from a Python host, using PyO3 + rquickjs (Rust bindings to QuickJS). Two layers:

1. `quickjs_rs._engine` — PyO3 Rust extension wrapping rquickjs. Single compiled `.so`.
2. `quickjs_rs` — user-facing Python API (`Runtime`, `Context`, `Handle`, `ModuleScope`).

**Scope: v0.4 active, v0.3 complete at tag v0.3.0.** v0.3 delivered the PyO3 + rquickjs rewrite (wasm layer removed, 114/114 tests green, §13.1 + §13.2 acceptance green). v0.4 adds ES module support via a composable `ModuleScope` type — see `spec/module-loading.md`. The v0.3 API is unchanged in v0.4; `ModuleScope` + `Context.install()` + `module=True` eval is additive.

This repo previously used a wasm-based architecture (wasmtime + C shim). v0.3's rewrite dropped the wasm layer entirely; the git history from the wasm era (v0.1, v0.2) is preserved and useful for understanding design decisions — the spec evolution story is traceable through commit bodies.

## Authoritative docs

- `spec/implementation.md` — the v0.3 rewrite spec. Section numbers (§3, §6, etc.) are referenced throughout.
- `spec/module-loading.md` — the v0.4 module-loading spec. Active — this is what phase 4 implements.
- `spec/benchmarks.md` — benchmark cases and targets.
- `spec/quickjs.wit` — archival; documents the original wasm interface contract. Not authoritative for this repo, but useful for understanding the API design rationale.

**The spec and the code never disagree.** If you need behavior that contradicts the spec, update the spec in the same commit that changes the code.

## Implementation order

**v0.4 active — follow §10 of `spec/module-loading.md`.** Eight steps: (1) Cargo.toml loader feature, (2) ModuleScope class with validation, (3) static registry + Context.install + single-file import green, (4) nested scopes + ./ relative imports, (5) resolver-boundary enforcement, (6) real ES-module eval path, (7) full test suite + §13.3 acceptance, (8) spec + CLAUDE.md updates → tag v0.4.0-rc1. The rhythm is the same as v0.1 / v0.2 / v0.3: one assertion green per commit, acceptance as the north star.

For archival context on earlier phases, `spec/implementation.md` §15 documents v0.3's phase 0–3 rewrite (PyO3 + rquickjs, all 114 tests + §13.1 + §13.2 acceptance green at tag v0.3.0).

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

From `spec/module-loading.md` §3.1 + §4 (module scopes + resolver — v0.4):
- Scopes are recursive. A ModuleScope can contain `str` values (files) and/or other ModuleScope values (dependencies) at any depth. No two-level cap; the dependency graph shape is whatever the user hands us.
- Two namespaces within a scope, distinguished by value type: `str` entries are files (addressed by relative specifiers, `./X` / `../X`); `ModuleScope` entries are named dependencies (addressed by bare specifiers). The namespaces don't cross.
- A scope with any `str` entries must have `index.js` — that's what a bare `import "scope-name"` resolves to. A pure-dependency container (only ModuleScope values) doesn't need an index.js.
- `str`-keyed entries MAY contain `/` in the key — they're POSIX-style paths within the scope's file tree (`"lib/util.js"`, `"tests/deep/nested.js"`). No validation on path shape; `posixpath.normpath` runs against this set at resolve time.
- Any key, any depth, must not start with `./` or `../` — those are import specifiers, not valid as dict keys.
- Resolver rule is scope-local. Identify the referrer's containing scope and its position within that scope. `./X` or `../X` → `posixpath.normpath(dirname(position) + "/" + X)`, then look up that path in the scope's `str` entries; if the normalized path starts with `../` it's escape-past-root → error. Bare `X` → look up `X` in the scope's `ModuleScope` entries only (never reaches str). No parent traversal, no sibling visibility, no root fallback.
- A scope that uses a dependency must declare it in its own dict. Shared deps are expressed by spreading (`**base.modules`) into each scope that needs them — each spread creates an independent canonical path, which QuickJS caches independently.
- Relative specifiers never match a ModuleScope entry; bare specifiers never match a str entry — wrong-namespace is always an error, even if the key name matches.
- `Context.install()` is additive. Multiple calls insert into the same backing store; no flag, no guard, no "already installed" error. Re-inserting a name that hasn't been imported yet overwrites the source.
- QuickJS caches modules per canonical path per context. Re-installing a name that has been imported is a silent no-op — the cached record wins. Document this as a caveat; don't try to defeat it.
- The backing store is per-runtime, not per-context. rquickjs's `set_loader` operates at the runtime level; all contexts on the same runtime see the same module set.

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
- Do not commit code with failing tests (except during v0.3 phase 0 where tests failed by design — the bridge was being swapped; that window is closed).
- Do not implement v0.5+ features. The v0.4 out-of-scope list lives in `spec/module-loading.md` §12 (dynamic resolvers, filesystem loading, hot-reload, `import.meta`, source maps, bytecode caching, three-level nesting, `HostModule` as a separate type). Any of those items may be the right call for v0.5 — they are not the right call for v0.4. If you find yourself reaching for one, stop and ask whether the current spec covers the use case first.

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
spec/implementation.md      v0.3 rewrite spec (completed at v0.3.0)
spec/module-loading.md      v0.4 module-loading spec (active)
spec/benchmarks.md          Benchmark cases and targets
spec/quickjs.wit            Archival interface contract
Cargo.toml                  Rust dependencies (rquickjs, PyO3)
src/lib.rs                  §6 — PyO3 module registration
src/{errors,marshal,        §6 subsystems (split in v0.3)
     host_fn,runtime,
     context,handle,
     reentrance}.rs
quickjs_rs/runtime.py       §7 — Runtime
quickjs_rs/context.py       §7 — Context + eval_async + install (v0.4)
quickjs_rs/handle.py        §7 — Handle
quickjs_rs/globals.py       §7 — Globals proxy
quickjs_rs/modules.py       v0.4 — ModuleScope (pending step 2)
quickjs_rs/errors.py        §9 — Exception hierarchy
tests/test_smoke.py         §13 — Acceptance tests
tests/test_modules.py       v0.4 module tests (pending step 7)
tests/test_*.py             Focused tests (transferred)
benchmarks/                 Benchmark suite (transferred)
```