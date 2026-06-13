# ADR 0001: Portable WASM Execution Plane

Date: 2026-06-12
Status: Proposed (Phase 0 of docs/repl-wasm-security-hardening-spec.md)
Deciders: architecture owner, security owner (TBD)

## Decision

Move the REPL's default JavaScript execution plane into a portable
WebAssembly artifact, `quickjs-core.wasm` (target `wasm32-wasip1`),
instantiated by host adapters in Python (`wasmtime-py`) and Node/browser
(plain TypeScript over the standard `WebAssembly` API). A Rust host
adapter is deferred until a Rust consumer exists; conformance runs
through the Python adapter (which drives the same Wasmtime engine) and
Rust-side fuzzing targets the codec/guest crates directly via cargo.
PyO3 and N-API are not the primary architecture.

The guest binding layer is `rquickjs` (over quickjs-ng), with per-call
drop-down to the re-exported raw sys layer (`rquickjs::qjs`) where the
safe API falls short — the pattern the native snapshot code already
uses. A hand-written C shim is foreclosed by the v0.2 retrospective
below; sys-bindings-everywhere is rejected because the WASM sandbox
contains binding bugs either way, so owning all the unsafe FFI
discipline buys risk without security return. The full design is
`docs/repl-wasm-security-hardening-spec.md`; this ADR records the
decision, the retrospective on our previous WASM implementation, and the
performance budget that governs the migration.

## Context

The shipped `quickjs_rs` package embeds quickjs-ng via rquickjs/PyO3 in
the host Python process. The threat model
(`.github/THREAT_MODEL.md`, "Memory Sandbox") is explicit that this
provides resource guards, not memory isolation: a memory-safety bug in
QuickJS, rquickjs, or the bridge is a host-process compromise. The
hardening goal is to make engine memory guest linear memory, so that an
engine compromise is contained by the WASM sandbox and host interaction
is protocol-mediated.

## Retrospective: why v0.2 (WASM) was abandoned, and why that does not decide this

This project has already been WASM once. v0.2 ran QuickJS-ng inside
`quickjs.wasm` behind a five-layer stack
(Python → `quickjs_wasm` bridge → wasmtime-py → Wasmtime → C shim →
QuickJS), with MessagePack marshaling on every boundary crossing. It was
removed in `736c528` and replaced by the native PyO3 implementation; the
v0.3 spec recorded the rationale verbatim:

> "1-second cold start from wasm JIT compilation, ~110 µs eval floor
> from four boundary crossings, 1800 lines of hand-written C shim with
> manual refcounting and msgpack encoding, and a build pipeline
> requiring WASI-SDK + CMake + wasm-opt."

Measured deltas from `ada54b4` (M-series Mac, Python 3.14, release
build), v0.2 wasm vs v0.3 native:

| Benchmark | v0.2 wasm | v0.3 native | Native speedup |
|---|---|---|---|
| `bench_runtime_create` | ~1.00 s | 8.61 µs | ~116,000× |
| `bench_eval_noop` | ~111 µs | 1.23 µs | ~90× |
| `bench_host_call_noop` | ~134 µs | 1.52 µs | ~88× |
| `bench_host_call_100x_loop` | ~7.5 ms | 23.42 µs | ~320× |
| `bench_marshal_int` | ~388 µs | 1.55 µs | ~250× |
| `bench_eval_fibonacci_30` | ~180 ms | 49.34 ms | ~3.6× |
| `bench_eval_async_noop` | ~1.0 ms | 53.89 µs | ~19× |

Why those numbers were what they were — and what changes this time:

- **~1 s cold start** was per-process JIT compilation of the wasm
  module. Wasmtime supports ahead-of-time compilation
  (`Module.serialize`/`deserialize`, `.cwasm`); the V1 plan ships or
  caches a precompiled artifact, making cold start a one-time-per-build
  cost rather than per-process. This alone removes the worst v0.2
  number. Browsers get the equivalent via compiled-module caching.
- **~110 µs eval floor** came from four boundary crossings plus
  MessagePack encode/decode per crossing through a hand-rolled bridge.
  The new ABI is one export call per operation with a compact tagged
  codec designed for the purpose. The floor will not reach native
  1.23 µs — boundary copies are inherent to the security model — but
  the v0.2 floor was an artifact of its protocol, not of WASM.
- **1,874 lines of C shim** with manual refcounting are replaced by a
  Rust core; memory-lifecycle bugs in the shim were a maintenance and
  security liability that no longer exists in that form.
- **WASI-SDK + CMake + wasm-opt** toolchain pain is reduced to a
  cargo-centric build, but not eliminated: QuickJS's C still compiles
  via a wasi sysroot. This is an accepted, contained cost (one build
  pipeline produces one artifact consumed by every host), and the
  reproducible-build requirement in the spec (Phase 7) makes it a
  supply-chain control rather than overhead.

The decisive difference is the reason for the architecture. v0.2's WASM
was incidental — chosen for architecture-independent wheels — so when
native was 90× faster and simpler to build, WASM had nothing left to
justify it. This time WASM **is** the requirement: it is the memory
isolation boundary the threat model lacks. Performance is a budget to
manage, not the decision criterion. The budget below exists so the
migration cannot be silently killed by native-parity comparisons, and
equally so that a regression that actually breaks the product is caught
by an agreed number rather than argued case by case.

## Performance budget (V1 gates, proposed)

Budgets bind the WASM plane measured on the same hardware class as the
`ada54b4` baselines. They are deliberately set from REPL workload
reality (agent tool calls tolerate milliseconds; humans tolerate more),
not from native parity:

| Metric | Budget | v0.2 actual | Native actual |
|---|---|---|---|
| Module load (precompiled, per process) | < 50 ms | ~1 s (JIT) | n/a |
| Runtime + context create (warm) | < 5 ms | ~1 s | 81 µs |
| Sync eval floor (`1 + 2`) | < 25 µs | ~111 µs | 1.23 µs |
| Sync host call round trip | < 25 µs | ~134 µs | 1.52 µs |
| 100 host calls in a JS loop | < 2.5 ms | ~7.5 ms | 23 µs |
| Marshal small scalar | < 10 µs | ~388 µs | 1.55 µs |
| Async eval noop | < 250 µs | ~1.0 ms | 54 µs |
| End-to-end REPL eval overhead vs native, p50 | < 1 ms | — | — |

Product-breaking threshold: if callback-heavy REPL workloads exceed the
end-to-end overhead budget after the Phase 2 codec and Phase 4 callback
machinery land, the batching options reserved in the spec (handle-op
batching, module prefetch) are exercised before any retreat from the
architecture is considered.

## De-risking already done

- **Epoch interruption under the GIL (go/no-go for the Python host):
  PASS.** `spikes/epoch_gil_spike.py` (wasmtime-py 45.0.0,
  Python 3.14.3, macOS arm64): an infinite wasm loop on the main thread
  was trapped 5 ms after a watchdog thread incremented the epoch
  (0.505 s elapsed vs 0.5 s watchdog delay), while an observer thread
  made progress (42 ticks) during the blocked call — proving
  wasmtime-py releases the GIL during wasm execution and preemptive
  timeout is real. Trap surfaces as `wasm trap: interrupt`,
  classifiable distinctly.
- **Node worker + SAB interrupt shape: PASS.**
  `spikes/node_worker_interrupt_spike.mjs` (Node 24.6.0): a
  worker-hosted wasm infinite loop whose interrupt check calls a host
  import (a JS closure reading a `SharedArrayBuffer` via
  `Atomics.load`) was interrupted 501 ms in when the main thread
  flipped the flag at 500 ms; a hostile loop with no interrupt check
  was killed by `worker.terminate()` at the backstop deadline. Notably
  this needs no shared wasm memory or threads feature — the SAB is read
  inside the import closure — so it works on plain `wasm32-wasip1`.
  Incidental measurement: ~69.8 M import calls in 488 ms (~7 ns per
  host-import crossing in V8), confirming that serialization, not the
  crossing itself, is where the callback budget goes.
- **Build feasibility**: a Phase 1 spike built `quickjs-core.wasm` for
  `wasm32-wasip1` (~1.2 MB) and produced the findings recorded in the
  spec (sync host-call import flow, quickjs-ng stack-limit behavior on
  wasi). The spike source was not preserved; Phase 1 re-executes it.

## Alternatives considered

- **Stay native, rely on process isolation only.** Keeps today's
  performance; leaves "engine bug = host compromise" inside every
  deployment that doesn't add OS isolation, and the threat model's
  worker-process guidance is advisory, not architectural. Rejected as
  the default; the native path may remain as an explicitly-labeled
  trusted-performance option (spec Phase 7).
- **V8/JSC isolates.** Stronger engines, but enormous dependency
  surface, no portable Python/browser embedding story, and isolation
  still shares the host address space. Rejected.
- **PyO3/N-API wrapping a per-language wasm build.** Duplicates
  resolver/marshaling/async logic per host — the exact divergence the
  shared-core design eliminates. Rejected as primary architecture
  (spec, "Why Not PyO3/N-API As The Center").

## Consequences

- The default deployment is `wasm-inproc`: the WASM sandbox is the
  isolation boundary, containing an engine memory-safety bug in guest
  linear memory without a separate process. Not needing
  process-per-execution for semi-trusted (agent-generated, internal)
  code is the improvement over the native path and the point of this
  effort. `wasm-worker-process` is reserved for the cases that warrant
  a second independent boundary — hostile/multi-tenant code, or a host
  holding secrets the guest must never reach — where the jailed process
  contains a WASM-runtime escape and Spectre-class leakage. Defaulting
  to the worker process for all "untrusted" code would negate the
  reason to adopt the WASM plane (see spec, Deployment Profiles).
- Host adapters become security-critical code with normative
  requirements (spec, "Host Adapter Security Requirements"), including
  differential fuzzing across the three codec implementations.
- The native package remains the baseline during migration; benchmarks
  keep both planes honest against the budget table above.
- Wheel/package size grows (wasmtime dependency for Python; wasm
  artifact in both packages). Accepted; the v0.2-era ~50 MB
  wasmtime-py install cost is acknowledged and not treated as a
  blocker for a security-motivated architecture.
