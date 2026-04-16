# CLAUDE.md

Instructions for Claude Code working in this repository. Read this file in full at the start of every session.

## Project

`quickjs-wasm` is a Python library for sandboxed JavaScript execution, hosted from Python via a WASI build of QuickJS. Three layers:

1. `quickjs.wasm` — QuickJS compiled with WASI-SDK plus a C shim
2. `quickjs_wasm._bridge` — wasmtime-py wiring
3. `quickjs_wasm` — public Python API (`Runtime`, `Context`, `Handle`)

## Authoritative docs

- `spec/implementation.md` — the complete implementation spec. Section numbers (§6, §7, etc.) are referenced throughout this file and in commit messages.
- `spec/quickjs.wit` — the interface contract between the shim and the bridge.
- `spec/design.md` — background and rationale. Not authoritative, but useful context for "why" questions.

**The spec and the code never disagree.** If you need behavior that contradicts the spec, update the spec in the same commit that changes the code. Never silently diverge.

## Implementation order

Follow §17.2 of `spec/implementation.md` for v0.2. The v0.1 order lives
in §17.1 as historical reference for the completed work.

Before writing code for any section, re-read that section of the spec.

## The north star

`tests/test_smoke.py` contains the acceptance tests from §13.

- `test_acceptance` (§13.1) — the v0.1 north star. Remains green through v0.2 and beyond.
- `test_async_acceptance` (§13.2) — the v0.2 north star. Every assertion represents functionality that must work for v0.2 to ship. Work through it assertion-by-assertion; each passing assertion is a natural commit boundary.

```bash
pytest tests/test_smoke.py -v   # check current status
```

## Non-negotiable invariants

From §6.4 (shim):
- All exported shim functions validate slot IDs; invalid slots return a negative status, never crash
- The per-context MessagePack scratch buffer is invalidated on every marshaling call — callers must read it fully before the next call
- Never expose raw `JSValue` structs across the wasm boundary; always go through slot IDs

From §7 (Python API):
- Handles are scoped to their creating context; cross-context use raises `InvalidHandleError`
- Every public method either returns successfully or raises a `QuickJSError` subclass — no bare exceptions, no `RuntimeError` leaking from the bridge
- `__del__` on a leaked handle emits `ResourceWarning` (Python convention)

From §8 (marshaling):
- MessagePack ext code 0 = `undefined` (empty body), code 1 = `bigint` (UTF-8 decimal string body)
- Python `int` in [-2^53+1, 2^53-1] → JS `number`; outside that range → `bigint`
- Numbers are always `float64` in MessagePack, even integer-valued — JS number semantics are f64

From §9 (safety):
- WASI is denied by default: no FS, no net, no real clock, no stdio, no env, no proc_exit
- Defaults are 64 MB memory / 1 MB stack / 5 s timeout
- Every added WASI capability needs a spec update and a justification

From §7.4 (async, v0.2):
- Only one eval_async in flight per context at a time. Second raises
  ConcurrentEvalError.
- Sync eval raises ConcurrentEvalError on the first attempt to drive an
  async-host-call promise. Eval fails cleanly before any JS runs
  against the pending promise.
- Async host-function detection is inspect.iscoroutinefunction (auto).
  ctx.register(..., is_async=True/False) is the explicit override.
- eval_async timeout is cumulative across calls on the same context,
  starting from context creation. timeout= kwarg on eval_async
  overrides per-call. Sync eval timeout is unchanged (per-call).
- Cancellation: catch CancelledError at the driving loop's await,
  cancel the internal TaskGroup, reject in-flight promises with
  HostCancellationError, run pending jobs one final time to let JS
  catch/finally execute, re-raise CancelledError unless JS absorbed it.
- HostCancellationError's JS-side name is the string literal
  "HostCancellationError" injected by the shim encoding path (same
  pattern as HostError). The Python class name matches by convention.

## Scope

v0.2 active. v0.1 complete at tag v0.1.0 (§13.1 acceptance green).
See §17.2 for v0.2 implementation order.

For features beyond v0.2 (see §14, §16), stub with NotImplementedError
referencing the spec section that defers them. Do not partially
implement v0.3+ features.

## Dependencies

Only what's in §12 of the spec. Do not add dependencies without updating §12 in the same commit. No JSON libraries as a substitute for MessagePack. No alternative wasm runtimes.

## Commands

```bash
# Build the wasm after changes to shim.c, the QuickJS submodule, or build config
cd wasm && ./build.sh && cd ..

# Install package in dev mode
pip install -e ".[dev]"

# Run tests
pytest                          # all
pytest tests/test_smoke.py      # acceptance test
pytest -x                       # stop on first failure
pytest -k handle                # run a subset by name

# Type check and lint
mypy quickjs_wasm
ruff check
```

## Commit discipline

**Linear history.** No merge commits on `main`. Configure once per clone:

```bash
git config pull.rebase true
git config merge.ff only
```

When integrating work from a branch or remote, rebase — never merge. The history should read as a linear sequence where each commit represents one meaningful unit of progress against the spec.

**One commit per meaningful unit of progress.** For this project, the natural unit is *"a new assertion in `tests/test_smoke.py` turns green"* or *"a self-contained section of the spec is fully implemented."* Do not commit intermediate flailing — experiments that didn't work out, partial attempts, save-points. If you tried something and reverted or rewrote it, use `git reset` or `git commit --amend` before committing (or before the next commit) so the final history reflects the decision, not the journey.

**Commit message format.** Subject line first, imperative mood, ~50 characters, with a scope prefix. Blank line. Body explaining *why* — the decision, the constraint, the spec section implemented, the test assertion it turns green.

```
shim: implement qjs_eval and the slot table

Implements §6.1 slot management and the qjs_eval export in §6.2.
Slot IDs cross the wasm boundary instead of raw JSValue structs
(per §6.4 invariant). The slot table is per-runtime and refcounted
to match QuickJS's own JSValue refcounting semantics.

Turns green: tests/test_smoke.py "ctx.eval('1 + 2') == 3".
Refs: spec/implementation.md §6.
```

Scope prefixes in use: `shim:`, `bridge:`, `api:`, `tests:`, `spec:`, `build:`, `ci:`, `docs:`. Pick the scope matching the primary change. If a commit spans multiple scopes, that's usually a sign it should be split.

**The body matters more than the subject.** The diff shows *what* changed; the body exists to explain *why*. Always reference the relevant spec section(s) — that's the thread that ties commits back to the authoritative doc. When a commit turns a test assertion green, name the assertion.

**Spec changes travel with code changes.** If you change the spec, say so explicitly in the commit body:

```
spec+shim: clarify slot lifetime on context teardown

§6.1 didn't specify what happens to outstanding slots when a context
is freed. They're now documented as invalidated, and the shim
enforces it. Prevents a class of use-after-free bugs in handle
lifetime tests.

Refs: spec/implementation.md §6.1.
```

Spec and code changes belong in the same commit when the code change prompted the spec clarification — that's the common case for this project and it's correct.

**Amend before pushing, never after.** Amending (`git commit --amend`) and force-pushing rewrites history others may have pulled. Amend freely on local commits to keep history tidy; once pushed, use a new commit instead.

## Before every commit

1. `pytest` passes — no test that was green goes red
2. `mypy quickjs_wasm` is clean
3. `ruff check` is clean
4. If `shim.c` or `vendor/quickjs-ng` changed, the wasm has been rebuilt and `quickjs_wasm/_resources/quickjs.wasm` is current
5. If the spec changed, it's included in this commit

Commits are always at a green state. No "WIP, tests failing" on the main branch.

## What not to do

- Do not rewrite the shim in Rust, Zig, or any language other than C. We are committed to C + WASI-SDK for v0.1 through v1.0.
- Do not change public API signatures in §7.2 without updating the spec in the same commit.
- Do not implement v0.3+ features (see §14) before v0.2 is complete and §13.2 acceptance is green.
- Do not swap MessagePack for JSON, Protobuf, or any other serialization format.
- Do not silently expand WASI permissions. Every added capability is a spec change.
- Do not use `RuntimeError` or bare `Exception` in public methods — only `QuickJSError` subclasses.
- Do not commit code with failing tests.
- Do not disable tests to make CI pass. Fix the code or update the spec.
- Do not add new PyPI dependencies without updating §12.

## When in doubt

If the spec is ambiguous or contradicts itself, stop and ask the user rather than guessing. Ambiguity in a spec is a bug in the spec — surfacing it is more valuable than picking an interpretation silently.

If an implementation approach requires a decision the spec doesn't cover (e.g. an internal data structure choice), pick the simpler option and note the choice in a code comment referencing the spec section it extends.

## Deferred-feature tests

Some tests use `pytest.raises(NotImplementedError)` to lock in behavior
for features the spec defers to a later version. When the deferred
version ships, these tests flip from "asserts raises" to "asserts works."

A feature-flip commit is always a spec-changing commit:
- The spec section that declared the deferral (e.g. §7.2 "raises
  NotImplementedError in v0.1") must be updated in the same commit.
- The test body flips from the raises assertion to the real assertions.
- The implementation lands.

Do not ship a feature-flip commit that leaves the spec claiming the
feature still raises NotImplementedError. That's a silent spec-code
disagreement of exactly the kind §"Authoritative docs" forbids.

## Spec-conformance tripwires

When implementing a design choice from the spec that isn't obvious
from reading the code — cancellation absorption semantics, budget
independence across sync/async evals, cleanup ordering, structural
detection over heuristics — add a single test whose docstring states
the choice and references the spec section.

The test exists not to prove "the feature works" (other tests cover
that) but to fail red if a future refactor silently reverts the
design choice. Growing test files accumulate a tripwire section
alongside functional tests.

Existing examples:

- `test_cancel_finally_host_calls_also_cancelled` — §7.4
  cancellation-during-cleanup: finally-block host calls are cancelled
  alongside the rest, rather than being allowed to delay cancellation
  indefinitely.
- `test_swallowed_host_raise_does_not_leak_cause_into_later_eval` —
  §10.2: the bridge's `_last_host_exception` side-channel is cleared
  at each sync-eval entry so a swallowed raise doesn't wrongly
  attach to an unrelated later `HostError`.
- `test_sync_eval_pure_js_promise_is_not_error` — §7.4: sync-eval-
  with-async-hostfn detection is dispatcher-level (flag set when a
  registered async host fn is invoked), not eval-return-type-level
  (returning a Promise doesn't, by itself, mean the user did
  something wrong).
- `test_sync_eval_does_not_decrement_cumulative_budget` — §7.4:
  sync eval and async eval have independent timeout accounting.
- `test_non_error_throw_coerces_to_jserror` — §10.1: `throw 'x'` /
  `throw 42` surface as `JSError(name='Error', message=ToString(x))`
  not as a missing-`.name` error.

## Integration tests are load-bearing

`test_smoke.py`'s §13.x acceptance tests are not redundant with the
focused tests in `test_async.py`, `test_async_host_functions.py`,
etc. The v0.2 §13.2 smoke test caught two real bugs in the
cancellation machinery that focused tests missed:

- Leaked `_pending_tasks` dict entries on cancel: focused tests
  cancel and check propagation, but don't inspect continuation
  state. The integration sequence (cancel → DeadlockError on a
  later call) did.
- TaskGroup scope gap for initial-eval dispatches: the TaskGroup
  only wrapped the driving loop, not the initial eval — so host
  tasks scheduled during the initial eval were bare
  `loop.create_task` children the TaskGroup never saw. Focused
  cancellation tests used "cancel mid-driving-loop" flows that
  dodged the gap.

Focused tests verify behavior through the API surface; integration
tests exercise continuation state and long sequences where behavior
composes. Do not skip or defer integration tests on the grounds
that "focused tests already cover this" — that reasoning produced
two production-quality bugs that would have shipped if v0.2 had
tagged without §13.2 integrated.

## Benchmarks

Performance benchmarks live in `benchmarks/`, separate from correctness tests in `tests/`.
The authoritative spec is `spec/benchmarks.md`.

**Benchmarks are not tests.** They measure time, not correctness. No assertions, no pass/fail
logic. If a benchmark's measured value changes, that's information — not a failure.

**Benchmarks are not optional.** Any change to the shim, bridge, marshaling, or eval pipeline
should be checked against benchmarks locally before committing. Not "run benchmarks and prove
it didn't regress" (wall-time variance makes that unreliable locally) — rather "run benchmarks,
look at the numbers, and note any surprising changes in the commit body."

### Commands

```bash
# Run all benchmarks
pytest benchmarks/ --codspeed

# Run a specific file
pytest benchmarks/test_startup.py --codspeed

# Run without codspeed (dry-run, no timing output)
pytest benchmarks/
```

### When to write a new benchmark

Add a benchmark when:
- A new public API method is added (eval_async got its own benchmarks in v0.2)
- A new marshaling path is added (bytes, bigint each got benchmarks)
- A performance-sensitive code path is refactored (the benchmark pins the before/after)
- A user reports a performance issue (the benchmark reproduces and tracks the fix)

Do not add a benchmark for:
- Error paths (error handling speed is not performance-critical)
- One-time operations (module loading, function registration — unless startup cost is the concern)
- Niche type combinations (benchmark the representative cases, not every permutation)

### Benchmark naming

Use `bench_` prefix, not `test_`. This makes benchmarks visually distinct from correctness tests
in output and file listings. The function name describes what's measured:

- `bench_eval_fibonacci_30` — what operation, what workload
- `bench_marshal_dict_flat_100` — what layer, what shape, what size
- `bench_host_call_100x_loop` — what crossing, what pattern, what scale

### Benchmark code patterns

Setup belongs outside the `benchmark()` call:

```python
# GOOD — only the eval is measured
def bench_eval_json_parse(benchmark, ctx):
    code = "JSON.parse('{\"a\": 1}')"
    benchmark(ctx.eval, code)

# BAD — context creation is measured alongside eval
def bench_eval_json_parse(benchmark):
    rt = Runtime()
    ctx = rt.new_context()
    benchmark(ctx.eval, "JSON.parse('{\"a\": 1}')")
```

Use fixtures from `benchmarks/conftest.py` for shared setup (runtime, context, pre-registered
host functions). Benchmarks should be fast to write — if the fixture doesn't exist for your
case, add it to conftest.

### Relationship to spec

`spec/benchmarks.md` §8 has order-of-magnitude targets for each benchmark category. If a
benchmark lands outside its expected range, either the range is wrong (update the spec) or
the implementation has an unexpected cost center (investigate before committing). The spec
is the reference for "does this number look right."

### CI

Benchmarks run in CI via CodSpeedHQ/action on every push to main and every PR. Results are
tracked on CodSpeed for regression detection. The workflow is `.github/workflows/benchmarks.yml`.

Do not modify the CI workflow to skip benchmarks or change the measurement mode without
updating `spec/benchmarks.md` §6. The mode (`walltime`) is a deliberate choice — see §6 for
the rationale.

## File map

```
spec/implementation.md      §1-17, the complete spec
spec/quickjs.wit            Interface contract
spec/design.md              Background (not authoritative)
vendor/quickjs-ng/          Vendored JS engine (submodule)
wasm/shim.c                 §6 — the C shim
wasm/shim.h                 §6.2 declarations
wasm/CMakeLists.txt         §4 build config
wasm/build.sh               §4.2 build pipeline
quickjs_wasm/_bridge.py     §7, §9 — wasmtime wiring
quickjs_wasm/_msgpack.py    §8 — marshaling
quickjs_wasm/runtime.py     §7.2 — Runtime class
quickjs_wasm/context.py     §7.2 — Context class
quickjs_wasm/handle.py      §7.2 — Handle class
quickjs_wasm/globals.py     §7.2 — Globals proxy
quickjs_wasm/errors.py      §10 — exception hierarchy
quickjs_wasm/_resources/quickjs.wasm   Built artifact
tests/test_smoke.py         §13.1 + §13.2 acceptance tests
tests/test_async.py         §11.1 — eval_async / eval_handle_async
tests/test_async_host_functions.py
                            §11.1 — async host fn registration,
                            cancellation, sync-eval-async-hostfn guard
tests/test_errors.py        §11.1 — exception-class conformance tripwires
tests/test_*.py             §11.1 v0.1 test files (primitives,
                            objects, handles, globals, limits,
                            exceptions, host_functions)
tests/shim/                 Shim-level integration tests (direct
                            Bridge access, not through Context)
```

## Quick reference — where things are specified

| Looking for | See |
|---|---|
| Public Python API | `spec/implementation.md` §7 |
| Wasm ABI | `spec/implementation.md` §6, `spec/quickjs.wit` |
| MessagePack format | `spec/implementation.md` §8 |
| Limits, WASI stubs | `spec/implementation.md` §9 |
| Error types and propagation | `spec/implementation.md` §10 |
| Async execution model (v0.2) | `spec/implementation.md` §7.4 |
| Async error types (v0.2) | `spec/implementation.md` §10.3 |
| Exception-propagation implementation notes | `spec/implementation.md` §10.4 |
| Test layout | `spec/implementation.md` §11 |
| Build toolchain | `spec/implementation.md` §4 |
| Acceptance criteria | `spec/implementation.md` §13.1 (v0.1), §13.2 (v0.2) |
| Implementation order | `spec/implementation.md` §17.1 (v0.1), §17.2 (v0.2) |
| What's in v0.1 / v0.2 vs later | `spec/implementation.md` §14, §16 |
