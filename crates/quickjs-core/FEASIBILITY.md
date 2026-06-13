# Build Feasibility Verdict — `quickjs-core.wasm`

Date: 2026-06-12
Status: **GREEN** — reproduced from source we control on the current toolchain.

This is the verdict the lost-spike-artifact reconnaissance could not give:
that artifact proved a build *had* happened ~2 days earlier but its source
was gone and the spec's decisions had since changed. The numbers below come
from a fresh `crates/quickjs-core` crate, clean build, current dependencies.

## Result

| Question | Answer |
|---|---|
| Builds from source we control today? | **Yes** — `cargo build --target wasm32-wasip1 --release`, 13.6s clean |
| `bindgen` / libclang required? | **No** — rquickjs-sys 0.11.0 ships wasip1-compatible bindings; C engine built via bundled `cc` |
| External wasi-sdk required? | **No** — no `toolchain/` install, no external sysroot |
| In-module QuickJS actually runs? | **Yes** — `qrs_selftest()` evals `1 + 2` → `3` under zero-capability WASI |
| Artifact size | **600 KB** (`opt-level="z"`, LTO, strip; minimal feature set, no host-call/ABI machinery yet) |
| Resolved versions | `rquickjs 0.11.0` (cargo notes 0.12.0 available), `rustc 1.95.0` |

## WASI import surface (zero-capability)

`clock_time_get`, `environ_get`, `environ_sizes_get`, `fd_close`,
`fd_fdstat_get`, `fd_seek`, `fd_write`, `proc_exit` — the small set the
spec's JS WASI shim must provide. No filesystem/network/preopen imports.

## Cross-check vs. reconnaissance

Lost artifact was ~1.2 MB with 29 exports (full handle ABI, host-call
machinery). This spike is 600 KB with 3 exports — smaller because it is
feasibility-only (no codec, no host imports) and built `opt-level="z"`.
The delta is explained by scope, not a toolchain regression; the size
budget (~1–1.2 MB, spec) remains plausible for the full surface.

## Time-sensitive findings still valid

- quickjs-ng **still disables its internal stack limit on `__wasi__`**
  (verified in the resolved `rquickjs-sys-0.11.0/quickjs/quickjs.c`):
  `update_stack_limit` sets `stack_limit = 0`. The spec's Stack finding
  and the Phase 1 shadow-stack remediation question are current, not
  stale.

## Reproduce

```
cd crates/quickjs-core
cargo build --target wasm32-wasip1 --release
python ../../spikes/build_feasibility_run.py \
  target/wasm32-wasip1/release/quickjs_core.wasm   # expects "PASS ... GREEN"
```

## Stack-check verdict (2026-06-12): RESTORABLE

The question: quickjs-ng disables its stack limit on `__wasi__`
(`update_stack_limit` → `stack_limit = 0`), so `JS_SetMaxStackSize` is a
no-op and unbounded JS recursion traps the instance instead of raising a
catchable error. Can engine-level checking be restored on wasip1?

**Yes — demonstrated end-to-end.**

- *Default behavior reproduced* (`qrs_recurse_depth`, unpatched build):
  recursion runs past the disabled limit and **traps** — `wasmtime.Trap`,
  instance dead. `set_max_stack_size(256 KB)` had no effect. Confirms the
  spec's concern empirically, not just from source.
- *Root cause confirmed*: only the threshold is neutered. The check
  machinery (`js_check_stack_overflow`) runs every call; it compares the
  stack pointer (`__builtin_frame_address(0)`) against `stack_limit`, and
  `__builtin_frame_address(0)` is valid on wasm (resolves into the
  linear-memory shadow stack — our `quickjs.c` already compiles and uses
  it). Upstream zeroes the limit out of caution, not because the
  primitive is unavailable.
- *Fix proven*: patched `update_stack_limit`'s `__wasi__` branch to use
  the normal `stack_top - stack_size` calculation, built via a vendored
  rquickjs-sys (`[patch.crates-io]`), re-ran the same probe. Recursion is
  now **catchable**: error raised at depth 1486 with a 256 KB QuickJS
  limit under a 1 MiB wasm stack; the instance survives and the eval
  returns normally. One-line semantic change, no toolchain change.

Consequence for the spec: the most agent-reachable trap path
(unbounded recursion) **can be closed**, returning it to a catchable
`InternalError` with a surviving context — native parity. Phase 1 should
adopt the patch; the delivery mechanism (vendored quickjs source vs.
build-time source rewrite in `quickjs-core-wasm-build`) is a Phase 1
implementation choice. The spike's vendored copy was removed after the
verdict; reproduce via the patch described above.

## Open (deliberately out of scope for feasibility)

- `rquickjs` 0.11 vs 0.12 — pin chosen at Phase 1 start, not here.
- Epoch interruption wired into this crate (proven generically by
  `spikes/epoch_gil_spike.py`; integration is Phase 1).
- Whether the shadow-stack stack-check can be restored — the Phase 1
  verdict item.
