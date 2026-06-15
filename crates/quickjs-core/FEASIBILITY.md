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
| Artifact size | **657 KB** on 0.12 (600 KB on 0.11; `opt-level="z"`, LTO, strip; minimal feature set, no host-call/ABI machinery yet) |
| Resolved versions | **`rquickjs 0.12.0`** (vendors quickjs-ng 0.15), `rustc 1.95.0` — pinned after the 0.11→0.12 reconciliation below |

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
  it). Upstream zeroes the limit because it had no working wasi mechanism,
  **not** because restoring it is unsafe (see Upstream discovery below).
- *Fix proven*: patched `update_stack_limit`'s `__wasi__` branch to use
  the normal `stack_top - stack_size` calculation, built via a vendored
  rquickjs-sys (`[patch.crates-io]`), re-ran the same probe. Recursion is
  now **catchable**: error raised at depth 1486 with a 256 KB QuickJS
  limit under a 1 MiB wasm stack; the instance survives and the eval
  returns normally. One-line semantic change, no toolchain change.

Consequence for the spec: the most agent-reachable trap path
(unbounded recursion) **can be closed**, returning it to a catchable
`InternalError` with a surviving context — native parity.

**Decision (2026-06-13): deferred upstream, not patched locally in V1.**
The fix is a one-line change to vendored quickjs-ng C, and rquickjs-sys
exposes no build-time hook to toggle the `#if defined(__wasi__)` branch
(no `-D` guard exists). The only local mechanisms are vendoring a forked
rquickjs-sys via `[patch.crates-io]` or mutating the registry cache —
both disproportionate for a one-line change. So V1 ships **no local
patch** and treats this as a known gap: unbounded recursion traps and
the instance is discarded (contained by discard-on-trap, not graceful).
The path to close it is upstream (quickjs-ng #774). The spike's vendored
copy was removed after the verdict; reproduce via the patch described
above.

### Upstream discovery (2026-06-13): the disable is an acknowledged gap, not a safeguard

Before patching around upstream, we researched *why* `__wasi__` zeroes the
stack limit. The finding strongly supports patching and gives us an
upstream contribution path:

- **The disable is not protective.** The `#if defined(__wasi__)` originally
  bracketed wasi *and* ASan together (PR #778 later split ASan out). It was
  never a fix for a wasi-specific hazard — it's a workaround for build
  environments where upstream had no working stack-pointer check.
- **The maintainer wants exactly our fix.** quickjs-ng issue
  [#774](https://github.com/quickjs-ng/quickjs/issues/774) ("Memory
  corruption in WASI build") reduces to a cyclic-array `toString()` that
  recurses without bound; on wasi it traps / corrupts memory instead of
  throwing `RangeError`. Maintainer (saghul): *"stack overflow detection
  is disabled in WASI, **I wonder if there is a way to make it work...**"*
  Our Spike D is that way: `__builtin_frame_address(0)` works on wasm
  (shadow stack), so `stack_top - stack_size` is meaningful.
- **A prior attempt to tidy this area was abandoned.** PR #637 ("Simplify
  disabling stack checks on WASI and ASAN") was **closed unmerged**
  ("Aha, maybe not a great idea after all..."), i.e. the area is known-
  unsatisfying and unresolved upstream.

Therefore: patch locally now (we are implementing the fix the maintainer
wished for, not overriding a deliberate safeguard), and **open an upstream
PR to quickjs-ng** restoring the wasi check via the shadow-stack approach —
which would also resolve #774. The local patch is interim, removable once
upstream ships; track it with a comment pointing at the upstream PR.

## Version pin: rquickjs 0.12 (reconciled 2026-06-12)

The spike originally inherited `0.11` from the native crate's pin. Bumped to
`0.12.0` (released 2026-05-26; vendors quickjs-ng 0.15) and re-validated:

- **Feasibility:** builds clean in 10.3s, still no bindgen/wasi-sdk;
  `qrs_selftest` → 3. Size 657 KB (vs 600 KB on 0.11).
- **Stack-check verdict holds:** quickjs-ng 0.15 still disables the limit on
  `__wasi__` (`update_stack_limit` → `stack_limit = 0`, now at line ~2759).
  Default build still traps; the same one-line patch makes recursion
  catchable (error at depth 1635 vs 0.11's 1486 — frame-layout difference,
  same outcome). The fix carries forward.

Decision: **pin 0.12 for `quickjs-core`.** It is a greenfield crate with no
migration cost, and 0.12 carries fixes and surfaces we want (below). The
native PyO3 crate keeps its own `0.11` pin independently.

Three 0.12 changes reconciled against the spec:

- **`Loader` trait now takes import attributes.** The guest module
  resolver/loader builds on this trait; the host-side resolution design must
  account for import attributes (`with { type: "json" }`) reaching the
  resolver as part of the edge. Noted in Module Loading.
- **Promise polling + GC assertion fixes.** Directly relevant to the
  async/poll eval state machine (Phase 4); a reason to be on 0.12, not 0.11.
- **`RQUICKJS_SYS_NO_WASI_SDK` env var (new in 0.12).** A wasi build-control
  knob; relevant to the `quickjs-core-wasm-build` pipeline. Noted in Build.

## Open (deliberately out of scope for feasibility)

- `rquickjs` 0.11 vs 0.12 — pin chosen at Phase 1 start, not here.
- Epoch interruption wired into this crate (proven generically by
  `spikes/epoch_gil_spike.py`; integration is Phase 1).
- Whether the shadow-stack stack-check can be restored — the Phase 1
  verdict item.
