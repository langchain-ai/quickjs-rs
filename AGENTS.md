# AGENTS.md

Repository conventions for coding agents and contributors.

## Project shape

- The engine is a **guest WASM module** (`crates/quickjs-wasm/` — rquickjs /
  quickjs-ng compiled to `wasm32-wasip1`) driven by **wasmtime-py**.
- `quickjs_rs/_engine.py` is the wasmtime-backed host adapter (`QjsRuntime`,
  `QjsContext`, `QjsHandle`, marshalling, the env imports).
- `quickjs_rs/` is the public Python API (`Runtime`, `Context`, `Handle`,
  `Snapshot`).
- WASM artifacts are built fresh from source and bundled into the package as
  `quickjs_rs/_guest.wasm` and `quickjs_rs/_transform.wasm` (gitignored, never
  committed); the wheel build hook (`hatch_build.py`) produces them via
  `scripts/build_guest.py`.

## Build and verification

- Build the wasm guests + bundle them: `python scripts/build_guest.py`
  (needs the Rust toolchain + `rustup target add wasm32-wasip1`).
- Install dev deps: `pip install -e ".[dev]"`
- Tests: `pytest`  (run async files per-file under a timeout; never wedge the suite)
- Lint: `ruff check .`
- Types: `mypy quickjs_rs`
- Benchmarks: `pytest benchmarks/ --codspeed`

## Runtime and API invariants

- Handles are context-bound and must error on cross-context use.
- Disposed handles must fail fast with `InvalidHandleError`.
- Public operations should either succeed or raise a `QuickJSError` subclass.
- Each `Context` is its own isolated wasm instance; `Runtime` is a config/factory.
- Async rules:
  - At most one `eval_async` in flight per context.
  - Sync eval must raise `ConcurrentEvalError` if an async host call is triggered.
  - Cancellation must reject pending host-call promises with `HostCancellationError`.
- Module rules:
  - ES modules resolve via a host loader callback pair:
    `rt.set_module_loader(normalize=, load=)`.
  - The host owns ALL resolution policy in `normalize` — there is no built-in
    scope tree / sandbox.
  - `.ts`/`.mts`/`.cts`/`.tsx` module sources are type-stripped by the host
    transform adapter (`quickjs_rs/_transform.py` driving `_transform.wasm`)
    before the QuickJS guest receives source. Hosts may override or extend this
    with public `SourceTransform` flags via `Runtime(transform_flags=...)`,
    per-eval `transform_flags=...`, or `rt.set_module_loader(transform_flags=...)`.
- Snapshots are whole-memory (entire guest heap; closures + pending promises
  survive); restore validates a fail-closed header incl. `build_id`.

## Engineering guardrails

- Do not add runtime dependencies casually; keep the package self-contained.
- Prefer rquickjs safe APIs over raw QuickJS C calls in the guest.
- Keep one source of truth across the host/guest boundary — no duplicated
  enums/tags/marshalling between `_engine.py` and the guest.
- Keep tests green before merge. Add or update tests when behavior changes.
