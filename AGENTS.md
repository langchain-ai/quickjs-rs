# AGENTS.md

Repository conventions for coding agents and contributors.

## Project shape

- `quickjs_rs._engine` is a Rust extension built with PyO3 + rquickjs.
- `quickjs_rs/` is the public Python API (`Runtime`, `Context`, `Handle`).
- Keep behavior consistent with tests and docs in this repository.

## Build and verification

- Dev build: `maturin develop --release`
- Install dev deps: `pip install -e ".[dev]"`
- Tests: `pytest`
- Lint: `ruff check .`
- Types: `mypy quickjs_rs`
- Benchmarks: `pytest benchmarks/ --codspeed`

## Runtime and API invariants

- Handles are context-bound and must error on cross-context use.
- Disposed handles must fail fast with `InvalidHandleError`.
- Public operations should either succeed or raise a `QuickJSError` subclass.
- Async rules:
  - At most one `eval_async` in flight per context.
  - Sync eval must raise `ConcurrentEvalError` if an async host call is triggered.
  - Cancellation must reject pending host-call promises with `HostCancellationError`.
- Module rules:
  - Module loading is handler-driven through `Runtime.set_import_handler(...)`.
  - The handler receives `(requested_key, referrer, specifier)` and returns source text or `None`; `referrer` is `None` for top-level eval.
  - Bare specifiers are passed to the handler unchanged. Relative specifiers are normalized against the referrer before the handler is called.
  - Already-imported modules remain cached by QuickJS.

## Engineering guardrails

- Do not add runtime dependencies casually; keep the wheel self-contained.
- Prefer rquickjs safe APIs over raw QuickJS C calls.
- Keep tests green before merge.
- Add or update tests when behavior changes.
