# WASM Hardening — Spike Report

Pre-implementation experiments validating the load-bearing assumptions in
`docs/repl-wasm-security-hardening-spec.md` and ADR 0001 before Phase 1
construction begins. Every spike here is committed, reproducible, and its
verdict is folded back into the spec.

**Status: all spikes GREEN — no open pre-implementation unknowns.**

## Environment

| Tool | Version |
|---|---|
| rustc | 1.95.0 (2026-04-14) |
| target | `wasm32-wasip1` |
| rquickjs | 0.11.0 (0.12.0 available; pin deferred to Phase 1) |
| wasmtime-py | 45.0.0 |
| Node | 24.6.0 |
| Python | 3.14.3 |
| Host | macOS arm64 |

Spike Python runs use a throwaway venv (`/tmp/qjs-spike-venv`) with
`wasmtime` only — not a project dependency.

## Summary

| # | Spike | Question | Verdict | Commit |
|---|---|---|---|---|
| A | Epoch interruption under the GIL | Can `wasmtime-py` preempt a runaway loop, or does the GIL block the watchdog? | **PASS** — GIL released during wasm exec | `17c1f4b` |
| B | Node worker + SAB interrupt | Does the worker-hosted timeout shape work on plain wasm? | **PASS** — interrupt + termination both work | `8b8ab72` |
| C | Build feasibility | Does rquickjs build to wasip1 from source we control today, and run? | **GREEN** — no bindgen, evals `1+2` | `219f6ae` |
| D | Stack-check verdict | Can engine-level recursion limits be restored on wasi? | **RESTORABLE** — recursion made catchable | `dc7425d` |
| E | Main-thread freeze | Does a hostile spin freeze a single-threaded host, and is the flag undeliverable there? | **CONFIRMED** — froze; worker control stayed responsive | (this turn) |

---

## Spike A — Epoch interruption under the GIL

**File:** `epoch_gil_spike.py` · **Gate:** go/no-go for the Python host.

**Why it mattered.** The spec's preemptive-timeout story for Python depends
on a watchdog *thread* incrementing the Wasmtime epoch while the main thread
is blocked inside a wasm call. If `wasmtime-py` held the GIL across wasm
execution, the watchdog could never run and a hostile `for(;;);` would hang
the process forever — the entire Python timeout mechanism would be fiction.

**Method.** A WAT module exporting an infinite loop (`(loop br 0)`), epoch
deadline of 1, a watchdog thread that increments the epoch after 500 ms, and
an observer thread counting progress. Run under an external hard timeout so a
GIL-held hang fails cleanly.

**Result.** Trap raised at **0.505 s** (vs 0.5 s watchdog delay); observer
made **42 ticks** while the main thread was blocked in wasm; trap surfaced as
`wasm trap: interrupt`. The GIL *is* released during wasm execution →
preemptive timeout is real.

```
python spikes/epoch_gil_spike.py    # expects PASS
```

---

## Spike B — Node worker + SharedArrayBuffer interrupt

**Files:** `node_worker_interrupt_spike.mjs`, `interrupt_spike.wat`,
`interrupt_spike.wasm`.

**Why it mattered.** The spec's default Node shape hosts the instance in a
`worker_threads` worker and signals timeouts via a flag the main thread
writes. This validates that the graceful interrupt *and* the termination
backstop both work, and that they need no exotic wasm features.

**Method.** A wasm loop whose interrupt check calls a host import reading a
`SharedArrayBuffer` flag (`Atomics.load`); the main thread flips the flag at
500 ms (graceful) and `terminate()`s at 1500 ms (backstop). Two modes:
`spin_cooperative` (checks the flag) and `spin_hostile` (never checks).

**Result.** Cooperative loop interrupted at **501 ms** (flag flip observed
mid-eval from outside the worker, ~69.8 M iterations); hostile loop killed by
`worker.terminate()` at the backstop. **Key finding:** the SAB is read inside
the import closure, so **no shared wasm memory and no threads feature are
needed** — works on plain `wasm32-wasip1`. Incidental: ~7 ns per host-import
crossing in V8, confirming serialization (not the crossing) dominates
callback cost.

```
node spikes/node_worker_interrupt_spike.mjs    # expects PASS
# rebuild the wasm from wat if needed:
python -c "import wasmtime; open('spikes/interrupt_spike.wasm','wb').write(wasmtime.wat2wasm(open('spikes/interrupt_spike.wat').read()))"
```

---

## Spike C — Build feasibility

**Files:** `build_feasibility_run.py`, `../crates/quickjs-core/` ·
**Full writeup:** `../crates/quickjs-core/FEASIBILITY.md`.

**Why it mattered.** The original Phase 1 spike's source was lost (only a
stale 1.2 MB artifact remained), and the spec's decisions had changed since.
The biggest open risk: does `rquickjs-sys` ship `wasm32-wasip1` bindings, or
does the build need `bindgen` + libclang (the native crate only ships
bindings for native wheel targets)?

**Method.** A fresh minimal `quickjs-core` crate links rquickjs and exports
`qrs_selftest()` which creates a Runtime + Context and evals `1 + 2`. Clean
build to wasip1, then instantiate under zero-capability WASI and call it.

**Result.** Builds clean in **13.6 s with no bindgen and no external
wasi-sdk** — rquickjs-sys ships wasip1 bindings and builds QuickJS via the
bundled `cc` path. `qrs_selftest()` returns **3** under zero-capability WASI:
the engine runs in-module. Artifact **600 KB** (`opt-level="z"`, minimal
surface). WASI import surface is the small zero-capability set the spec
predicts (`clock_time_get`, `fd_write`, `environ_*`, `proc_exit`).

```
cd crates/quickjs-core && cargo build --target wasm32-wasip1 --release
python ../../spikes/build_feasibility_run.py \
  target/wasm32-wasip1/release/quickjs_core.wasm    # expects GREEN
```

---

## Spike D — Stack-check verdict

**File:** `stack_check_run.py` · **probe:** `qrs_recurse_depth` in
`../crates/quickjs-core/src/lib.rs` · **writeup:** `FEASIBILITY.md`.

**Why it mattered.** quickjs-ng disables its internal stack limit on
`__wasi__` (`update_stack_limit` → `stack_limit = 0`), so `JS_SetMaxStackSize`
is a no-op and unbounded recursion — a one-line, common bug in
agent-generated code — traps the instance instead of raising a catchable
error. This is the most agent-reachable trap path; whether it can be closed
gates the Phase 2 error taxonomy.

**Method.** `qrs_recurse_depth` evals self-recursion in `try/catch` and
returns the depth at which QuickJS raised, or the harness observes a
`wasmtime.Trap`. Measured (1) default behavior, then (2) a build with
`update_stack_limit`'s `__wasi__` branch patched to the normal
`stack_top - stack_size` calculation, via a vendored `rquickjs-sys`.

**Result.** Default: **traps** — instance dead, `set_max_stack_size` confirmed
a no-op. Root cause is narrow — only the threshold is zeroed; the check runs
every call and `__builtin_frame_address(0)` is valid on wasm (shadow stack).
Patched: recursion is **catchable** — error at **depth 1486** with a 256 KB
QuickJS limit under a 1 MiB wasm stack, instance survives, eval returns
normally. One-line semantic change, no toolchain change.

**Verdict: RESTORABLE.** Phase 1 adopts the patch (delivery mechanism —
vendored source vs. build-time rewrite — is a Phase 1 choice); the spike's
vendored copy was removed after the verdict. The #1 trap path closes,
regaining native recursion semantics.

```
# default (traps):
cd crates/quickjs-core && cargo build --target wasm32-wasip1 --release
python ../../spikes/stack_check_run.py \
  target/wasm32-wasip1/release/quickjs_core.wasm 1048576   # expects TRAP verdict
# the patched-build result is reproduced per FEASIBILITY.md "Stack-check verdict"
```

---

## Spike E — Main-thread freeze (placement rule)

**File:** `main_thread_freeze_spike.mjs` · reuses `interrupt_spike.wasm`.

**Why it mattered.** The spec's placement rule and delivery bullet assert
that a hostile synchronous spin on a single-threaded JS host freezes the
event loop, and that the cooperative interrupt flag is *undeliverable* there
because nothing on the one thread can run to set it — which is the whole
reason the Node adapter defaults to worker hosting. Asserted but never shown.

**Method.** Three cases, each main-thread case in a forked child with a hard
kill (a frozen main thread can't end itself). Each arms a 50 ms heartbeat and
a 200 ms timer that sets the interrupt flag, then enters a spin. (1) hostile
spin (`spin_hostile`, never checks the import); (2) cooperative spin
(`spin_cooperative`, *would* honor the flag); (3) worker control hosting the
hostile spin while the main thread heartbeats and then `terminate()`s.

**Result.**

| Case | heartbeats during spin | flag-setter ran | spin returned |
|---|---|---|---|
| main-thread hostile | **0** | **no** | no |
| main-thread cooperative | **0** | **no** | no |
| worker control | **10** | n/a | killed by `terminate()` |

The cooperative case is the sharp finding: even a loop that *would* obey the
flag is unsavable on a single thread, because the timer that sets the flag
cannot run while the synchronous call holds the stack. Cooperativeness is
irrelevant without a second thread to deliver the signal. The worker control
stayed responsive (10 beats) and was killable — the shape the spec mandates.

```
node spikes/main_thread_freeze_spike.mjs    # expects PASS
```

---

## What these did and did not establish

**Established:** the Python timeout mechanism is real (A); the Node timeout
shape works on plain wasm (B); the rquickjs binding builds and runs to wasip1
from source we control with no surprise toolchain dependency (C); the worst
agent-reachable trap path is closeable (D); single-threaded hosting is unsafe
for hostile spins regardless of loop cooperativeness, and worker hosting is
what makes the timeout story real (E).

**Not in scope (Phase 1 construction):** the full ABI surface, the wire
codec, host-side module resolution, the Python/TS adapters, and integrating
the epoch + stack-check fixes into the real `quickjs-core` crate (proven
generically here, wired up in Phase 1).
