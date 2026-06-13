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

## Open (deliberately out of scope for feasibility)

- `rquickjs` 0.11 vs 0.12 — pin chosen at Phase 1 start, not here.
- Epoch interruption wired into this crate (proven generically by
  `spikes/epoch_gil_spike.py`; integration is Phase 1).
- Whether the shadow-stack stack-check can be restored — the Phase 1
  verdict item.
