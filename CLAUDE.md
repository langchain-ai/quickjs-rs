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

`tests/test_smoke.py` contains the acceptance test from §13. Every assertion in it represents functionality that must work for v0.1 to ship. Work through it assertion-by-assertion. Each passing assertion is a natural commit boundary.

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
tests/test_smoke.py         §13 acceptance test
tests/test_*.py             §11.1 test files
```

## Quick reference — where things are specified

| Looking for | See |
|---|---|
| Public Python API | `spec/implementation.md` §7 |
| Wasm ABI | `spec/implementation.md` §6, `spec/quickjs.wit` |
| MessagePack format | `spec/implementation.md` §8 |
| Limits, WASI stubs | `spec/implementation.md` §9 |
| Error types and propagation | `spec/implementation.md` §10 |
| Test layout | `spec/implementation.md` §11 |
| Build toolchain | `spec/implementation.md` §4 |
| Acceptance criteria | `spec/implementation.md` §13 |
| What's in v0.1 vs later | `spec/implementation.md` §14, §16 |
