# OXC-Only TypeScript Transpile Step Spec

## Goal
Replace the current `oxidase + oxc` TypeScript install-time path with an OXC-only Rust pipeline to simplify the dependency graph and avoid odd pinning constraints.

## Current State
- TypeScript stripping/transforms occur in `src/modules.rs` via `oxidase::transpile`.
- The project also depends on `oxc_*` crates for snapshot name extraction (`src/ast.rs`).
- Today this creates extra dependency/source-identity complexity in `Cargo.toml` and `Cargo.lock`.

## Proposed Behavior (No API Change)
- Keep transpilation trigger exactly the same: only `.ts`, `.mts`, `.cts`, `.tsx` module keys are transformed.
- Keep error timing the same: TS parse/transform errors surface during `Runtime.install()`.
- Keep import specifiers unchanged (`"./x.ts"` stays `./x.ts`) to preserve current resolver behavior.
- Keep TS feature support used by tests/docs: enum runtime transform, namespaces, parameter properties, type erasure.
- Keep no-type-checking semantics (`tsc --noEmit` remains external).

## Implementation Plan
1. Add `src/transpile.rs` with a dedicated OXC pipeline entrypoint, e.g.:
   - `pub(crate) fn transpile_typescript(key: &str, source: &str) -> Result<String, String>`
2. Move TS decision logic out of `maybe_strip_ts` in `src/modules.rs` to call the new module.
3. Implement pipeline:
   - Parse with `oxc_parser` using TS/TSX `SourceType`
   - Build semantic scoping with `oxc_semantic::SemanticBuilder`
   - Transform with `oxc_transformer::Transformer::build_with_scoping`
   - Emit JS with `oxc_codegen::Codegen`
4. Configure transformer options explicitly:
   - `typescript.allow_namespaces = true`
   - `typescript.rewrite_import_extensions = None`
   - defaults for the rest unless behavior tests require changes
5. Preserve panic/error mapping:
   - wrap transpile path with `catch_unwind`
   - map parser/transform diagnostics to current `QuickJSError`-compatible strings

## Dependency Plan
1. Remove `oxidase` from `Cargo.toml`.
2. Use one coherent OXC dependency set (prefer crates.io exact versions for `oxc_allocator`, `oxc_ast`, `oxc_parser`, `oxc_span`, plus `oxc_semantic`, `oxc_transformer`, `oxc_codegen`).
3. Regenerate `Cargo.lock`.
4. Update Cargo lock git-source policy if needed (`.github/scripts/check_cargo_lock_git_sources.py`) so policy matches the new source strategy.

## Critical Constraint
`Cargo.toml` currently sets `rust-version = "1.75"`. Recent OXC releases may require a newer Rust toolchain.

Decision gate before implementation:
- either bump project MSRV/toolchain policy,
- or select an older crates.io OXC line that is compatible with project MSRV constraints.

## Files Expected to Change
- `Cargo.toml`
- `Cargo.lock`
- `src/modules.rs`
- `src/transpile.rs` (new)
- `README.md`
- `tests/test_modules.py`
- `.github/THREAT_MODEL.md`
- `.github/scripts/check_cargo_lock_git_sources.py` (if policy changes)

## Validation
1. Run:
   - `maturin develop --release`
   - `pytest`
   - `ruff check .`
   - `mypy quickjs_rs`
2. Confirm TS behavior parity with existing tests:
   - enum runtime values
   - namespace transform behavior
   - parameter properties
   - `.ts` extension preservation in static and dynamic imports
   - install-time TS syntax error surfacing
3. Confirm lockfile no longer includes `oxidase` and does not contain duplicate OXC graphs.

## Rollout Strategy
1. Land OXC transpile implementation behind current behavior parity tests.
2. Remove `oxidase` dependency once parity is green.
3. Update docs/security model text in same PR.
4. Run full CI matrix and benchmark checks.

## Reference Sources
- PR context: <https://github.com/langchain-ai/quickjs-rs/pull/21>
- OXC transformer crate docs: <https://docs.rs/oxc_transformer/latest/oxc_transformer/>
- `Transformer` API: <https://docs.rs/oxc_transformer/latest/oxc_transformer/struct.Transformer.html>
- `TypeScriptOptions`: <https://docs.rs/oxc_transformer/latest/oxc_transformer/struct.TypeScriptOptions.html>
- `RewriteExtensionsMode`: <https://docs.rs/oxc_transformer/latest/oxc_transformer/enum.RewriteExtensionsMode.html>
- OXC parser parse options: <https://docs.rs/oxc_parser/latest/oxc_parser/struct.ParseOptions.html>
- OXC repo activity: <https://github.com/oxc-project/oxc>
- Oxidase repo activity: <https://github.com/branchseer/oxidase>
