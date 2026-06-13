# REPL WASM Execution Plane Security Hardening Spec

Date: 2026-06-10 (revised 2026-06-12)
Status: Draft for architecture review
Owner: TBD

## Summary

Move the REPL's default code-execution plane to a portable WebAssembly artifact:

```text
quickjs-core.wasm
  -> Rust execution core
  -> QuickJS engine and JS heap inside WASM linear memory
```

Host languages should not link directly against QuickJS or native `rquickjs`. Instead, host packages instantiate `quickjs-core.wasm`, communicate through a stable import/export ABI, and expose language-native ergonomics:

```text
Python API      -> Python WASM host adapter   -> quickjs-core.wasm
Node/TS API     -> JS/WASM host adapter       -> quickjs-core.wasm
```

This is a security hardening effort. The goal is to stop running the JavaScript engine and its object heap in the same directly addressable memory space as the host application. The WASM module becomes the REPL execution plane; host callbacks, module loading, async settlement, and value transfer happen through explicit capabilities and copied byte payloads.

PyO3 and N-API are not the primary architecture in this model. They may still be useful as optional convenience wrappers around a host adapter, but they should not be required for the core security story. `wasm-bindgen` can be useful for JavaScript packaging, but it should not define the portable core ABI.

## Goals

- Make WASM the default execution plane for REPL JavaScript and TypeScript-derived JavaScript.
- Keep QuickJS engine state, JS heap state, persistent handles, pending Promise resolvers, and module evaluation state inside WASM guest memory.
- Expose a portable ABI that can be driven by Python, Node, browser, Rust, and other WASM-capable hosts.
- Preserve the current public semantics where practical: `Runtime`, `Context`, `Handle`, the runtime import handler, sync/async eval, host functions, dynamic import, error taxonomy, and cancellation behavior.
- Keep host capabilities explicit: callbacks, module loading, time, cancellation, and optional diagnostics should cross through declared imports/events.
- Avoid duplicating resolver, marshaling, and async state-machine logic across the Python and JS packages.
- Maintain a path to stronger OS/process isolation for high-risk untrusted workloads.

## Non-Goals

- Claiming in-process WASM is equivalent to a separate process, container, VM, or seccomp profile.
- Making privileged host callbacks safe by default. Host callbacks remain capabilities.
- Giving guest JavaScript ambient filesystem, network, process, environment, or clock access.
- Building browser support first if it forces weaker server-side semantics.
- Preserving native QuickJS performance exactly.
- Preserving the current PyO3 internal structure.
- Making `wasm-bindgen` the lowest-level ABI for all hosts.

## Current Problem

The current `quickjs-rs` implementation embeds quickjs-ng through Rust/PyO3 in the Python process. That is fast and convenient, but it is not a host-memory isolation boundary. A memory-safety vulnerability in QuickJS, quickjs-ng, `rquickjs`, or the native bridge could compromise the host process.

Existing mitigations such as QuickJS memory limits, stack limits, interrupt handlers, host-mediated module loading, and no implicit host APIs are valuable, but they do not address same-address-space engine compromise.

The desired hardening change is to move the REPL execution plane into WebAssembly so engine memory is guest linear memory and host interaction is protocol-mediated.

## Target Architecture

### High-Level Shape

```text
                 +-----------------------+
Python           | quickjs_rs Python API |
JS (Node/browser)| quickjs JS/TS API     |
                 +-----------+-----------+
                             |
                             v
                 +-----------------------+
                 | Host Adapter          |
                 | - load wasm           |
                 | - write/read memory   |
                 | - callback registry   |
                 | - async scheduling    |
                 | - host capabilities   |
                 +-----------+-----------+
                             |
                   imports/exports + bytes
                             |
                             v
                 +-----------------------+
                 | quickjs-core.wasm     |
                 | - Rust core           |
                 | - QuickJS             |
                 | - JS heap             |
                 | - handle table        |
                 | - resolver state      |
                 | - pending promises    |
                 +-----------------------+
```

### Package Layout

```text
crates/
  quickjs-core/              # no PyO3/N-API; compiles to wasm; owns execution semantics
  quickjs-core-abi/          # shared ABI structs/codecs; usable by hosts and guest
  quickjs-core-wasm-build/   # build/package glue for quickjs-core.wasm

python/
  quickjs_rs/                # Python public API over a WASM runtime adapter

js/
  quickjs-wasm/              # isomorphic Node + browser TypeScript package over the standard WebAssembly API
```

There is no separate browser package or browser build artifact. The JS package is isomorphic; environment differences (byte loading, worker backstop) live behind conditional exports. See JS Host Adapter.

Optional later packages:

```text
quickjs-host-rust/           # Rust host adapter using Wasmtime directly; deferred until a Rust consumer exists (see Rust Host Adapter)
python-native/               # optional PyO3 convenience host wrapper, if needed
node-native/                 # optional N-API convenience host wrapper, if needed
quickjs-wasm-web/            # optional browser-sugar package (prebuilt worker bundles, COOP/COEP helpers), only if a concrete need emerges
```

Those native packages would still call the same `quickjs-core.wasm`. They are not the core execution plane.

## Design Principles

- **WASM-first core:** `quickjs-core` must compile cleanly to a WASM target and cannot depend on CPython, Node, PyO3, N-API, or native host pointers.
- **Portable ABI:** every host must be able to call exports and read/write guest linear memory.
- **Copied values:** no host object pointers, guest pointers, `JSValue` pointers, `rquickjs::Value`, Python objects, or V8 values cross the boundary.
- **Opaque IDs:** runtimes, contexts, handles, functions, modules, and pending Promise settlements are identified by IDs plus generations where needed.
- **Explicit capabilities:** guest access to callbacks, modules, time, cancellation, and diagnostics is mediated through imports/events.
- **Host-neutral async:** avoid relying on runtime-specific async imports. Prefer an event/poll/settle protocol that works in Python, Node, browsers, and Rust.
- **One semantics layer:** module resolution, value encoding, handle validation, and error classification should live in shared Rust/ABI code where possible.

## Why Not PyO3/N-API As The Center

PyO3 builds native Python extension modules. N-API builds native Node addons. Both are useful for native embedding, but they do not provide a universal WebAssembly execution plane.

If the target is any host that can instantiate WebAssembly, the lowest common denominator should be:

- WASM exports,
- WASM imports,
- linear memory reads/writes,
- a stable binary protocol.

PyO3 and N-API may wrap that adapter later for ergonomics or performance in specific environments, but they should not define the core architecture.

## Why Not wasm-bindgen As The Core ABI

`wasm-bindgen` is excellent for JavaScript and browser integration. It generates JS glue and maps Rust concepts to JS concepts. The JS package does not need it — the ABI is raw exports plus linear memory, so plain TypeScript glue suffices — but it could be useful for optional packaging sugar later.

It is not ideal as the core ABI because Python, Rust hosts, and non-JS hosts should not need JS glue or JS-specific `JsValue` semantics. The portable ABI should be raw exports plus serialized buffers, with optional `wasm-bindgen` glue layered on top for JS consumers.

## WASM Target Choice

The core should start with the most constrained target that can support QuickJS reliably.

Candidate targets:

| Target | Pros | Cons |
|---|---|---|
| `wasm32-unknown-unknown` | Smallest ambient surface; maximum portability | Harder C/libc integration; may require custom allocator/shims |
| `wasm32-wasip1` | More practical C/Rust runtime support | Implies WASI imports; must ensure no ambient capabilities |
| `wasm32-wasip1-threads` | Better future path for thread-capable hosts | Lower portability; not all hosts support threads |

Recommendation:

1. Spike `wasm32-wasip1` first if QuickJS/libc integration is materially easier.
2. Configure zero ambient WASI capabilities: no preopened dirs, no inherited env, no network, no stdio unless explicitly needed for diagnostics.
3. Revisit `wasm32-unknown-unknown` after the ABI and QuickJS build are proven.

The choice does not require a browser-specific artifact: browsers instantiate the same `wasm32-wasip1` module through the standard `WebAssembly` API, with the JS package's zero-capability WASI shim supplying the `wasi_snapshot_preview1` imports (see JS Host Adapter).

## Core ABI

### ABI Versioning

Every host must check the ABI version before using the module.

```text
qrs_abi_version() -> u32
qrs_abi_features(out_ptr) -> status
```

Versioning rules:

- Additive fields require feature flags or minor ABI bump.
- Wire-incompatible changes require major ABI bump.
- Guest and host must fail fast on unsupported ABI versions.

### Memory Management Exports

```text
qrs_alloc(len: u32, align: u32) -> u32
qrs_free(ptr: u32, len: u32, align: u32) -> status
qrs_response_free(ptr: u32, len: u32) -> status
```

Host flow:

1. Encode request bytes.
2. Allocate guest input memory.
3. Copy request bytes into guest memory.
4. Call exported function.
5. Read returned response descriptor.
6. Copy response bytes out.
7. Free guest allocations.

### Response Descriptor

Use a fixed-size response descriptor written into guest memory or returned through an out pointer:

```text
struct AbiResponse {
  status: u32,
  tag: u32,
  ptr: u32,
  len: u32,
}
```

Status values:

```text
0 = ok
1 = guest_error_response
2 = invalid_request
3 = invalid_runtime
4 = invalid_context
5 = invalid_handle
6 = unsupported
7 = resource_exhausted
8 = guest_panic
9 = abi_mismatch
```

### Runtime And Context Exports

```text
qrs_runtime_new(config_ptr, config_len, out_response_ptr) -> status
qrs_runtime_close(runtime_id, out_response_ptr) -> status
qrs_context_new(runtime_id, config_ptr, config_len, out_response_ptr) -> status
qrs_context_close(context_id, out_response_ptr) -> status
qrs_runtime_gc(runtime_id, out_response_ptr) -> status
qrs_runtime_memory_usage(runtime_id, out_response_ptr) -> status
```

Runtime config should include:

- memory limit,
- stack limit,
- max handle count,
- max pending host calls,
- optional feature flags,
- optional diagnostic mode.

Context config should include:

- timeout policy,
- strict mode defaults if needed,
- module behavior flags,
- max marshal depth.

### Eval Exports

```text
qrs_eval(context_id, request_ptr, request_len, out_response_ptr) -> status
qrs_eval_handle(context_id, request_ptr, request_len, out_response_ptr) -> status
qrs_eval_start(context_id, request_ptr, request_len, out_response_ptr) -> status
qrs_eval_poll(eval_id, out_response_ptr) -> status
qrs_eval_cancel(eval_id, reason_ptr, reason_len, out_response_ptr) -> status
```

`qrs_eval` can be convenience sync eval for code that does not suspend on host callbacks.

`qrs_eval_start`/`qrs_eval_poll` should be the portable async model. It can represent:

- completed value,
- JS exception,
- timeout,
- pending host callback event,
- deadlock/pending promise with no work,
- cancellation absorbed by JS.

### Handle Exports

```text
qrs_handle_dispose(handle_id, generation, out_response_ptr) -> status
qrs_handle_dup(handle_id, generation, out_response_ptr) -> status
qrs_handle_type_of(handle_id, generation, out_response_ptr) -> status
qrs_handle_is_promise(handle_id, generation, out_response_ptr) -> status
qrs_handle_get(handle_id, generation, key_ptr, key_len, out_response_ptr) -> status
qrs_handle_get_index(handle_id, generation, index, out_response_ptr) -> status
qrs_handle_set(handle_id, generation, key_ptr, key_len, value_ptr, value_len, out_response_ptr) -> status
qrs_handle_has(handle_id, generation, key_ptr, key_len, out_response_ptr) -> status
qrs_handle_call(handle_id, generation, args_ptr, args_len, out_response_ptr) -> status
qrs_handle_call_method(handle_id, generation, name_ptr, name_len, args_ptr, args_len, out_response_ptr) -> status
qrs_handle_new(handle_id, generation, args_ptr, args_len, out_response_ptr) -> status
qrs_handle_to_value(handle_id, generation, options_ptr, options_len, out_response_ptr) -> status
```

Handle invariants:

- Handles are context-bound.
- Handles include generation checks.
- Disposed handles fail fast.
- Handles never reveal raw guest pointers or QuickJS pointers.
- Host adapters must dispose handles deterministically where possible and warn on leaked handles if the host language supports it.

### Module Exports

Module resolution and source acquisition are host-mediated through the eval poll state machine. The host owns resolution (specifier + referrer -> canonical key) and source lookup; the guest owns the edge-resolution cache, module cache, and compile/link. See the Module Loading section for the full design.

```text
qrs_module_provide(eval_id, request_id, flags, key_ptr, key_len, source_ptr, source_len, out_response_ptr) -> status
qrs_module_cache_clear(context_id, request_ptr, request_len, out_response_ptr) -> status
```

`qrs_module_provide` settles a pending `ModuleRequest` event from `qrs_eval_poll`:

- `flags = resolve`: `key` is the canonical key for the requested edge and `source` always carries the module's UTF-8 source. If the key is unregistered, the guest registers, optionally type-strips, compiles, and caches it. If the key is already registered, the settlement links the existing instance; the guest verifies the provided source matches the registered module's source (hash compare) and fails the settlement with `invalid_request` on mismatch.
- `flags = miss`: the host cannot resolve this edge; the guest raises the import error inside JS.

`qrs_module_cache_clear` invalidates the guest's edge-resolution cache and module cache (all keys, or the keys named in the request payload). It is the host's invalidation lever and part of the V1 ABI — with host-side resolution it is the only way for a host to change a prior resolution answer.

Settlements are validated against the originating eval and request ID. A `qrs_module_provide` call with an unknown, already-settled, or foreign `request_id` fails with `invalid_request` and must not affect any other pending import.

### Snapshot Exports

```text
qrs_snapshot_create(context_id, request_ptr, request_len, out_response_ptr) -> status
qrs_snapshot_restore(context_id, snapshot_ptr, snapshot_len, out_response_ptr) -> status
qrs_snapshot_validate(snapshot_ptr, snapshot_len, out_response_ptr) -> status
```

Snapshot compatibility must be decided explicitly:

- Native-produced snapshots may not be compatible with WASM-produced snapshots.
- Cross-version snapshot compatibility should be feature-gated and tested before promising it.
- Snapshot payloads should include ABI version, QuickJS build identity, flags, and feature markers.

Snapshot bytes are code-equivalent. Restore deserializes engine state, and `JS_ReadObject`-style bytecode loading is not hardened against malicious input: a malicious snapshot owns the restoring instance and everything in its trust domain. Rules:

- Restore only snapshots produced in the same trust domain, or whose content is independently authenticated.
- Snapshots persisted outside the producing process must carry a MAC or signature that the host adapter verifies before calling `qrs_snapshot_restore`. `qrs_snapshot_validate` checks structure and compatibility, not trustworthiness — it must not be presented as a security control.

## Wire Protocol

### Encoding

Start with a compact tagged binary format implemented in shared Rust code. Keep a debug JSON mode for tests and diagnostics.

Do not use host-language object serialization as the core format. Avoid Python pickle, JS structured clone, and host-specific object encodings.

### Value Model

```text
Value =
  Null
  Undefined
  Bool(bool)
  Number(f64)
  BigInt(decimal_string)
  String(utf8)
  Bytes(Vec<u8>)
  Array(Vec<Value>)
  Object(Vec<(String, Value)>)
  Handle { context_id, handle_id, generation, type_name }
  Error { name, message, stack }
```

Rules:

- Preserve current recursion depth limits.
- Preserve root vs nested `undefined` semantics.
- BigInt crosses as decimal string to avoid precision loss.
- Bytes cross as bytes, not base64, in the binary protocol.
- Object properties should preserve insertion order where QuickJS exposes it.
- Cycles are not supported in normal value marshaling; handles should be used for opaque object graphs.

### Request/Response Envelope

Every request should include:

```text
Envelope {
  abi_version,
  request_id,
  kind,
  flags,
  payload,
}
```

Every response should include:

```text
Response {
  request_id,
  status,
  payload,
  diagnostics?,
}
```

Request IDs are useful for debugging, async host-call correlation, and test traces.

## Host Callback Model

Host callbacks remain capabilities. Registering a host function is equivalent to giving guest JS a way to ask the host to perform work.

### Registration

Host adapter API:

```text
register(name, fn, is_async=False)
```

Core registration payload:

```text
HostFnSpec {
  context_id,
  fn_id,
  name,
  mode: sync | async,
}
```

Guest stores only `fn_id`, name, mode, and callback trampoline state. The actual callable remains in the host adapter registry.

### Sync Host Call Flow

**Decision (Phase 4):** sync host calls use the direct import flow, not
the event/poll model. The poll model cannot suspend a synchronous JS call
stack — QuickJS has no way to pause mid-call — so a poll-based sync flow
would force sync host functions to return Promises, breaking native API
parity (`hostFn(2) + 1` must work in sync eval, a stated compatibility
goal). The import flow needs **no reentrant export calls**: the host only
reads/writes guest memory from inside the import, which every candidate
runtime (Wasmtime, wasmtime-py, browser `WebAssembly`) supports.

```text
guest imports (module "quickjs_host"):
  host_call_sync(context_id, fn_id, args_ptr, args_len) -> code:u32 << 32 | result_len:u32
  host_call_sync_read(dst_ptr) -> status

1. JS calls registered sync function.
2. Guest trampoline marshals args to ABI Value bytes (guest memory).
3. Guest calls host_call_sync; the host import reads the args from guest
   memory, runs the callback from its registry, and stashes the encoded
   result host-side (code 0), or records the original error host-side and
   returns code 1 (sanitized) / code 2 (unknown fn_id).
4. On code 0 the guest allocates result_len bytes from its own allocator
   and calls host_call_sync_read to copy the result in, then converts it
   to a JS value.
5. On codes 1/2 the guest throws the sanitized
   `HostError: Host function failed` / `unknown host function`.
```

Because the flow is import-based, sync host functions work in plain
`qrs_eval`, in polled evals, and inside handle calls alike.

### Async Host Call Flow

```text
1. JS calls registered async host function.
2. Guest creates a JS Promise and pending_id.
3. Guest stores promise resolve/reject in guest state.
4. qrs_eval_poll returns HostCallAsync { fn_id, pending_id, args }.
5. Host schedules coroutine/Promise/future.
6. qrs_eval_poll may return Pending while host work is running.
7. Host calls qrs_resolve_pending(context_id, pending_id, value_bytes) or qrs_reject_pending(...).
8. Host calls qrs_eval_poll again to drain QuickJS jobs.
9. Eval completes, rejects, times out, deadlocks, or remains pending.
```

This model maps cleanly to:

- Python `asyncio`,
- JavaScript Promises,
- Rust futures,
- browser event loops,
- synchronous hosts that choose to block.

### Cancellation

Cancellation requirements:

- Host can call `qrs_eval_cancel(eval_id, reason)`.
- Guest rejects in-flight host-call promises with `HostCancellationError`.
- Guest runs/drains jobs so JS `catch`/`finally` can observe cancellation.
- If JS absorbs cancellation and returns a value, host receives fulfilled result.
- If JS does not absorb cancellation, host receives cancellation result.

### Host Error Sanitization

Current behavior should be preserved:

- JS-visible host error is sanitized: `HostError: Host function failed`.
- Python/host caller may receive the original host exception if uncaught at eval boundary.
- Original exception details should not be exposed to guest JS unless explicitly configured.

The ABI should represent both:

```text
GuestErrorRecord { name, message, stack? }
HostDiagnosticRecord { opaque_id, host_type, sanitized_message, debug? }
```

Diagnostic details are host-controlled and should be disabled by default for untrusted code.

## Eval State Machine

The portable host should drive eval as a state machine:

```text
EvalStarted(eval_id)
EvalPoll -> Completed(Value)        # Value::Handle when started with EVAL_FLAG_HANDLE_RESULT
EvalPoll -> Threw(ErrorRecord)
EvalPoll -> HostCallAsync(event)
EvalPoll -> ModuleRequest(event)    # Phase 5
EvalPoll -> Pending
EvalPoll -> Deadlock
EvalPoll -> Cancelled
```

This avoids assuming that every host can block, use native threads, or provide async imports.

Notes from implementation:

- Sync host calls never appear as poll events — they ride the direct
  import flow (see Sync Host Call Flow).
- Timeout is not a poll state: deadlines are enforced host-side via epoch
  interruption, which traps mid-execution rather than returning.
- At most one polled eval per context (native "one eval_async in flight"
  parity); a second `qrs_eval_start` fails with `ConcurrentEvalError`,
  as does an async host call dispatched from plain `qrs_eval`.

## TypeScript Support

There are two separate TypeScript concerns.

### TypeScript Source Executed By REPL

QuickJS executes JavaScript, not TypeScript. TypeScript support should remain a transform step before evaluation.

Current behavior:

- Module sources whose requested key ends in `.ts`, `.mts`, `.cts`, `.tsx` are stripped/transpiled when the import handler provides them.
- TypeScript syntax errors surface while resolving the import.
- No type checking is performed.

Target behavior:

```text
TypeScript source (returned by host resolver)
  -> provided to guest via qrs_module_provide
  -> Rust stripping/transpile step, likely still oxidase initially
  -> JavaScript source
  -> registered in quickjs-core.wasm module cache
  -> evaluated by QuickJS inside WASM
```

Open design choice:

- Run TypeScript stripping inside `quickjs-core.wasm` for maximum portability and consistency.
- Or run stripping in host adapters/shared host code before sending JS to the core.

Recommendation: run stripping inside `quickjs-core.wasm` if build size and performance are acceptable. This keeps behavior identical across Python, Node, browser, and Rust hosts. If oxidase or parser dependencies make the guest too large, move stripping into a shared host-side package with conformance tests.

### Node/TypeScript Host SDK

A TypeScript application can use a package that loads `quickjs-core.wasm` and exposes TypeScript types/classes.

```text
import { Runtime } from "@quickjs-rs/wasm";

const rt = new Runtime();
const ctx = rt.newContext();
const result = ctx.eval("1 + 2");
```

Implementation:

- Plain TypeScript over the standard `WebAssembly` API; the same code runs in Node and browsers.
- No `wasm-bindgen` glue and no N-API in the primary package.

## Python Adapter

The Python package should keep the current public API where possible:

```python
from quickjs_rs import Runtime

with Runtime() as rt:
    with rt.new_context() as ctx:
        assert ctx.eval("1 + 2") == 3
```

Internally:

```text
Python Runtime
  -> WasmModule instance
  -> qrs_runtime_new

Python Context
  -> context_id
  -> qrs_context_new

Python Handle
  -> context_id + handle_id + generation
  -> qrs_handle_* exports
```

### Python Bridge Flow

```text
1. Encode request as ABI bytes.
2. Allocate guest memory with qrs_alloc.
3. Copy bytes into guest memory.
4. Call qrs_* export.
5. Read AbiResponse descriptor.
6. Copy response bytes from guest memory.
7. Decode response.
8. Free guest response memory.
9. Raise Python exception or return Python value.
```

### Python Runtime Choices

Potential runtime choices:

- `wasmtime-py`, likely the first spike target.
- `wasmer`/`wasmedge` Python bindings if portability or package size is better.
- A very thin optional native wrapper that vendors Wasmtime, if dependency ergonomics become a blocker.

The first implementation should optimize for correctness and security clarity, not minimal wheel size.

### Python Host Functions

The Python adapter owns:

- callback registry,
- fn_id allocation,
- `inspect.iscoroutinefunction` / explicit async detection,
- `asyncio` task scheduling,
- cancellation propagation,
- converting host exceptions into sanitized guest errors plus host diagnostics.

The WASM core owns:

- JS function trampoline objects,
- pending Promise table,
- `pending_id` allocation or validation,
- promise resolve/reject handles,
- job queue draining,
- sync-eval hit async-host-call detection.

## JS Host Adapter (Node And Browser)

One isomorphic TypeScript package serves both Node and browsers. Because the ABI is raw exports plus linear memory, the glue is plain TypeScript over the standard `WebAssembly` API, and none of it is environment-specific: encode request, `qrs_alloc`, copy into `memory.buffer`, call export, read descriptor, copy out, decode, drive the poll loop. There is no separate browser package and no separate browser build artifact. The sync export + event/poll design means no JSPI or Asyncify is required in browsers.

Package responsibilities:

- Load and instantiate `quickjs-core.wasm`.
- Provide TypeScript declarations.
- Encode/decode ABI values.
- Manage the callback registry and module resolver (including the default resolver).
- Map async host functions to Promises.
- Drive the eval poll state machine on the event loop.
- Integrate with timers/`AbortSignal` for cancellation and timeout.
- No N-API and no `wasm-bindgen` in the primary package.

Environment differences are confined to a small conditional-exports seam (`"node"` / `"default"` entry points):

- Byte loading: filesystem read in Node; `fetch`/`instantiateStreaming` in browsers. The API should also accept caller-provided bytes (`BufferSource | Response | URL`) to sidestep loader policy entirely.
- Worker hosting (the default) and termination backstop: `worker_threads` in Node, Web Worker in browsers, with the interrupt flag in a `SharedArrayBuffer` (see CPU And Timeouts).

WASI shim rules:

- The package ships one small pure-JS zero-capability `wasi_snapshot_preview1` shim used in **both** Node and browsers. Do not use `node:wasi`: the guest must see an identical import environment in every JS host.
- With zero ambient capabilities configured, the expected import surface is small: `clock_time_get`, `random_get`, `fd_write` (panic/diagnostic output only), `environ_*` stubs, `proc_exit`.
- Clock and randomness are policy points: the shim must support coarsening or fixing `clock_time_get` and deterministic `random_get` for deployments that want to withhold timing capabilities from guest JS. This raises the bar against timing side channels but is not a Spectre boundary — a JS attacker can build a timer from a SharedArrayBuffer counter-thread, and the worker-hosted timeout path ships a SAB (see Deployment Profiles, secret co-residency).
- Tradeoff: deterministic `random_get` makes QuickJS hash seeds predictable, enabling hash-flooding DoS by a guest that can choose object keys. Default to real randomness with a coarsened clock; deterministic mode is for reproducibility-controlled deployments that accept this risk, and the docs must say so.

Browser is a conformance target, not a package: CI runs the same package's conformance suite in a headless browser. It remains a distinct target because timer, memory, threading, and interruption behavior differ from server hosts (see Resource Controls).

## Rust Host Adapter (Deferred)

V1 ships Python and TypeScript adapters only. A `quickjs-host-rust` adapter (Wasmtime directly, typed wrappers over ABI calls, callback trait registry, async driver) is deferred until a Rust consumer actually exists — maintaining a third adapter is real cost with no V1 user.

Its former roles are covered without it:

- Conformance tests run through the Python adapter; `wasmtime-py` drives the same Wasmtime engine, so engine-level behaviors (epoch interruption, store limits, traps) are exercised through the same code paths.
- Rust-side fuzzing targets the codec and guest crates directly (`cargo fuzz` against `quickjs-core-abi` decoders and `quickjs-core` internals); a host adapter was never required for that.
- Differential fuzzing keeps three codec implementations without it: the guest/reference Rust codec (driven by a cargo harness), the Python host codec, and the TypeScript host codec.
- Guest ABI debugging happens through the Python adapter. If that proves too slow an iteration loop in practice, add a minimal internal Rust test harness in the workspace — internal tooling, not a public adapter surface.

## Module Loading

`ModuleScope` no longer exists. The current native architecture is a runtime-level dynamic import handler; the WASM design preserves its observable semantics but changes the division of labor (decision 2026-06-12): **resolution is a host concern.** The host resolver is a graph-building algorithm — each import statement is an edge `(referrer, specifier)`, the host resolves it to a canonical node (`canonical_key`), and provides the node's source the first time the node appears. The engine never interprets specifiers; module identity is entirely host-minted.

```text
resolver(specifier, referrer_key | none) -> { canonical_key, source } | miss
```

- `specifier` is the raw text written in the import statement, untouched.
- `referrer_key` is the canonical key of the importing module, or none for top-level eval. Every referrer key was itself minted by the host in an earlier resolution, so the host can reconstruct the full import chain from edges it has already seen — the event does not carry a call stack.
- `canonical_key` is the node identity: the module cache is keyed on it, and it becomes the `referrer_key` for imports inside that module. Aliases/namespaces (e.g. `@module` mapping into a packaged file tree) are expressed by resolving different edges to the same or different canonical keys — edge labels never become identities.
- `source` is always present: the resolver is a pure function of the edge and needs no knowledge of guest cache state. The guest decides what to do with it — unregistered key: type-strip, compile, cache; registered key: link the existing module instance (this is how aliases and import cycles converge) and discard the provided source after verifying it matches the registered module's source (hash compare). A mismatch is a determinism violation and fails the settlement with `invalid_request` — consistency is guest-enforced, not honor-system.
- `miss` raises the import error inside JS, preserving the current error shape.
- TypeScript keys (`.ts`, `.mts`, `.cts`, `.tsx`) are type-stripped at registration, as before.

Flow through the poll protocol:

```text
1. Guest QuickJS hits an import edge with no cached resolution (static or dynamic import).
2. qrs_eval_poll returns ModuleRequest { request_id, specifier, referrer_key | none }.
3. Host adapter invokes the resolver.
4. Host calls qrs_module_provide(eval_id, request_id, canonical_key, source | miss).
5. Guest caches the edge resolution, registers/type-strips/compiles the module if new, links, resumes.
6. Host continues qrs_eval_poll.
```

Division of responsibility:

- Guest owns: the **edge-resolution cache** (`(referrer_key, specifier) -> canonical_key`), the **module cache** (`canonical_key -> instance`), compile/link, TypeScript stripping, import error shape.
- Host owns: resolution and source lookup. The resolver is a capability like any host callback; no resolver registered means imports fail.
- Adapters ship a **default resolver** preserving current native semantics — relative specifiers resolve path-wise against the referrer key, bare specifiers pass through as their own canonical key — so hosts that don't care about namespacing see no behavior change. The default resolver's algorithm is pinned by the conformance corpus in every adapter; an old-style `handler(requested_key, referrer, specifier) -> source | miss` wraps directly on top of it.

Rules and consequences:

- The host is consulted **once per unique edge** per context; repeat edges and repeat nodes are guest-local. Edges ≥ nodes, so first-build round trips slightly exceed the previous once-per-key design, and every settlement copies full source into guest memory even when the node is already cached (the price of a stateless resolver). Module-heavy startup cost, including redundant-source copies for alias-heavy graphs, must be benchmarked (see Performance Benchmarks).
- **Determinism rule:** within a context, the resolver must answer a given edge consistently; the guest's edge cache enforces at-most-once consultation per edge, and the always-present source lets the guest enforce content consistency across edges (hash compare on registered keys, `invalid_request` on mismatch). `qrs_module_cache_clear` (clears both caches) is the host's only lever to re-resolve. A host that wants different answers over time clears and rebuilds; mid-graph identity changes are not expressible.
- `specifier` and `referrer_key` in events are guest-emitted bytes: attacker-controlled under engine compromise. Host adapters must treat both as untrusted strings (no implicit path/URL interpretation), and referrer-based *policy* (e.g. "only `@charts` internals may import `@charts/private`") is advisory — the enforceable security gate is what source the host ever provides, which remains fully host-controlled.
- An optional batch/prefetch form (host resolves several edges in one settlement) may be added later if round-trip cost shows up in benchmarks; it must not change resolution semantics.

## Resource Controls

### Memory

Controls:

- QuickJS memory limit inside guest. First line of defense — but it is enforced by the engine itself, so it does not hold under engine compromise.
- WASM linear memory maximum configured by the host runtime (e.g. Wasmtime store limits). **Required, not optional:** this is the limit that holds when the engine is compromised. Adapters set it by default; unlimited memory requires explicit opt-in.
- Host adapter caps (required defaults, host-enforced before parsing or allocation):

| Cap | Suggested default |
|---|---|
| Sync host-call argument bytes | 8 MiB |
| Response payload bytes | 32 MiB |
| Module source bytes | 16 MiB |
| Handles per context | 10,000 |
| Pending host calls per context | 1,000 |
| Contexts per runtime | 64 |
| Runtimes per instance | 16 |

Defaults are starting points to be tuned against real REPL workloads; the requirement is that every cap exists, is on by default, and raising it is an explicit configuration act.

WASM linear memory never shrinks: a long-lived instance retains its peak footprint. Adapters must expose an instance-recycling policy for long-lived deployments and document peak-retention behavior.

Polling is deadline-bounded: adapters must not drive `qrs_eval_poll` without a deadline. A guest that returns `Pending` indefinitely is terminated by the deadline like any other timeout.

Required tests:

- runaway allocation raises `MemoryLimitError`, not host OOM;
- linear-memory growth beyond the store limit traps and is classified distinctly from QuickJS `MemoryLimitError`;
- large ABI response is rejected predictably;
- each adapter cap above rejects predictably at its boundary;
- handle leaks are bounded by configured max handle count.

### CPU And Timeouts

Portable timeout model:

- Host tracks deadlines.
- The graceful tier is uniform across hosts: the guest QuickJS interrupt handler calls an imported `host_interrupt() -> i32` on its interpreter cadence. The import reads host-side state the guest cannot touch — an atomic flag in the Python host (host memory, safely settable from any thread), `Atomics.load` on a parent-written `SharedArrayBuffer` in worker-hosted JS. When it returns nonzero, QuickJS unwinds cleanly and the eval returns `TimeoutError`; **the instance survives**, preserving native timeout semantics for the common case. A plain hostile `for(;;);` is caught here, not by a trap.
- Eval poll loop surfaces `Timeout` when the deadline expires while the eval is suspended.

Cooperative checks alone cannot stop a hostile `for(;;);` — but not because the check doesn't run. The QuickJS interpreter polls the interrupt handler on a counter cadence inside the bytecode loop; pure JS cannot dodge it. A cooperative timeout fails on the other two conditions it needs:

- **Delivery:** the handler only reads a flag — someone must be able to *set* it while the host thread is blocked inside the wasm call. Instance memory is not writable cross-thread on Wasmtime hosts (`Store` is not thread-shareable), and on a main-thread JS host nothing can run at all (event loop blocked; host microtasks cannot drain while the wasm call holds the stack). The purpose-built channel is the `host_interrupt` import above: it reads *host-side* state, which another host thread can always set safely. Delivery therefore needs (a) the import channel and (b) a second thread to write it — main-thread JS hosting lacks the second thread, hence the worker default.
- **This applies to polled evals too.** Every `qrs_*` call is a synchronous slice; between slices the host runs freely and checks deadlines, but the guest controls slice length. Guest-internal promise/microtask churn does not end a slice — QuickJS drains its own job queue inside the call, so `while (true) await Promise.resolve();` is one unbounded slice, indistinguishable from `for(;;);`. Only events that need the host (async host call, module request, completion) end a slice. Sync vs. async eval changes typical exposure, not the worst case; the mechanisms below are sized to the worst case.
- **Placement rule:** a slice blocks the thread that runs it — inherent to wasm (and to the current native engine; `ctx.eval` blocks its thread today). Adapters must place the instance on a thread whose blocking is acceptable: worker-hosted by default in JS (the parent event loop never runs a slice and stays responsive); off the asyncio event loop (executor thread) in the async Python adapter. `wasmtime-py` releases the GIL during slices (verified), so only the calling thread blocks in Python.
- **Coverage and trust:** checks are skipped during long native-builtin stretches (e.g. pathological regex between check points), and a compromised engine simply never consults the handler.

Each supported host must therefore have a named preemption mechanism that fixes at least delivery. This is a V1 requirement, not an optional hardening layer. Epoch interruption fixes all three conditions (thread-safe by design via `Engine.increment_epoch`; checks compiler-inserted at every loop backedge in generated machine code, so neither native-builtin loops nor a compromised engine can skip them). The SAB flag fixes delivery only — coverage and trust gaps remain — which is why JS hosts also require the termination backstop, which needs no guest cooperation at all:

| Host | Required mechanism | Strength |
|---|---|---|
| Python (`wasmtime-py`) | Graceful: `host_interrupt` import reading a host-side atomic. Escalation: epoch interruption (`Engine.increment_epoch` from a watchdog thread) | Graceful tier preserves the instance; epoch trap is preemptive and discards it |
| Node | Worker-hosted instance (default); graceful: `host_interrupt` import reading a parent-written `SharedArrayBuffer`. Escalation: worker termination | Graceful tier preserves the instance; termination destroys it |
| Browser | Same shape via Web Worker (`SharedArrayBuffer` requires COOP/COEP). Escalation: Web Worker termination | Graceful tier preserves the instance; termination destroys it |

Rules:

- An epoch/fuel trap tears down the eval and surfaces `TimeoutError`. A trap leaves the QuickJS heap in an arbitrary mid-mutation state: **the instance is poisoned and must be discarded — always.** "Recover to a verified-consistent state" is not an option; it is not cheaply verifiable and a wrong answer is an isolation failure. Adapter APIs must make post-trap recycling cheap and automatic.
- Worker termination is a backstop, not a timeout mechanism: it loses all runtime state. Adapters relying on it must document that a tight-loop timeout destroys the instance.
- **Backstop trigger:** termination fires only on deadline-expiry-without-response — the parent flips the SAB flag at the deadline (graceful attempt), waits a configured grace window, and terminates only if the slice still hasn't ended (cooperative check unreachable: native-builtin loop, compromised engine, or broken flag path). Resource limits never escalate to termination: heap-limit errors, store-memory traps, and stack traps all end the slice on their own and are handled by the normal error/discard rules. Termination exists solely for the one failure that never ends the slice — unbounded CPU.

Trap recovery and reachability:

- A trapped instance is never repaired in place. The adapter surfaces a trap event and makes re-instantiation cheap; session continuity, if any, is **application policy** built on the existing snapshot primitives or eval-log replay — out of scope for this spec, and adapters must not checkpoint by default.
- What matters for this spec is **how reachable the trap path is from ordinary guest JS**, because every reachable path converts a recoverable error into instance loss:
  - **Unbounded recursion** is today the easiest path — a one-line, common bug in agent-generated code — because quickjs-ng disables its internal stack limit on `__wasi__` (see Stack). **Phase 1 must investigate restoring engine-level stack checking on wasi** (address-based checks against the linear-memory shadow stack; locals live there, so `&local` comparison should be viable). If restored, recursion returns to a catchable `InternalError` with a surviving context, and this path closes.
  - **Compute-bound native builtins** (catastrophic regex backtracking, large BigInt exponentiation, primitive-array sorts, large JSON operations) stall inside C code where the interrupt cadence may not reach, riding out the grace window. The conformance corpus must measure interrupt coverage of these paths; any builtin that does not honor the interrupt handler within the grace window is documented as trap-reachable.
  - Remaining triggers — store-memory traps (fire only if QuickJS's own accounting failed first: a compromise indicator), guest panics, engine exploits — are not reachable from well-formed guest JS and are the cases discard-on-trap exists for.
- Choosing a host WASM runtime without epoch-or-equivalent interruption is not acceptable for V1 server-side hosts.

The cooperative interrupt flag has a sharp limitation in single-threaded JS hosts: while wasm executes synchronously, the event loop is blocked and nothing in the same thread can flip the flag. The flag helps only in polled evals, between poll steps. Therefore:

- The Node adapter defaults to hosting the instance in a `worker_threads` worker, with the interrupt flag in a `SharedArrayBuffer` written from the main thread, and worker termination as the backstop. Main-thread hosting is opt-in and documented as having no mid-eval timeout for sync eval.
- The browser story is the same shape with a Web Worker. `SharedArrayBuffer` requires cross-origin isolation (COOP/COEP headers); the package documentation must cover this, and the adapter must degrade explicitly (documented weaker timeout story), not silently, when isolation is unavailable.

Cancellation and deadlines cannot preempt host code: if the deadline expires while a sync host callback is executing, nothing happens until the callback returns. Host callbacks should enforce their own internal timeouts; adapter documentation must state this gap.

Phase 1 must demonstrate the Python mechanism against an infinite loop before any callback or handle work begins (see Phase 1 exit criteria). High-risk deployments should still use worker processes/containers regardless.

### Stack

Controls:

- WASM runtime stack guard (e.g. Wasmtime `max_wasm_stack`).
- Tests for recursion/stack overflow classification.

Phase 1 finding: quickjs-ng **disables its internal stack limit on
`__wasi__`** (`update_stack_limit` sets `stack_limit = 0`; verified in
the vendored source at `rquickjs-sys/quickjs/quickjs.c`), so
`JS_SetMaxStackSize` is a no-op inside the guest. Note `__wasi__` is a
compile-target define: building for `wasm32-wasip1` sets it even though
we grant zero WASI capabilities at runtime — the disabled branch is
compiled in regardless of runtime posture. Upstream disables the check
because the generic path calibrates against a runtime-sampled stack
address, and wasm's primary execution stack is not addressable
(address-of-local yields the linear-memory shadow stack instead). On the WASM plane the
runtime stack guard is the enforcement layer, and stack overflow is a
trap that poisons the instance — a documented deviation from native
semantics, where it is a catchable `InternalError` and the context
survives. Hosts classify the trap distinctly (e.g. `StackOverflow`, not
a generic panic). Phase 1 investigates restoring the check in our build
(shadow-stack address comparison — see Trap recovery and reachability)
rather than waiting on upstream, since unbounded recursion is the most
agent-reachable trap path.

## Security Model

### What Improves

- QuickJS heap and engine state move to WASM linear memory.
- Host memory is not directly addressable by guest code under normal WASM sandbox rules.
- Host capabilities become explicit imports/events.
- Python/Node objects do not enter QuickJS directly.
- The same hardened core can be used across hosts.

### What Does Not Improve Automatically

- A WASM runtime vulnerability can still break isolation.
- A bug in the ABI codec can expose capabilities or corrupt protocol state.
- Host callbacks can still leak data or perform privileged actions.
- In-process WASM does not isolate process-level CPU/RSS failure as strongly as OS isolation.
- Browser/Node/Python runtimes differ in interruption and memory controls.
- In-process WASM does not stop speculative-execution (Spectre-class) reads of host memory. Cranelift enables mitigations by default and withholding timing capabilities raises the bar, but neither is a boundary (see Deployment Profiles for why clock-withholding is probabilistic against a JS attacker). High-value secrets resident in the host process (API keys, tokens, credentials) must be assumed reachable in principle by a hostile guest sharing the address space. Do not co-locate high-value secrets with hostile guests.

### Host Adapter Security Requirements

Under this architecture the host adapter inherits the security-kernel role: it is the code standing between a potentially compromised guest and the host process. Once engine compromise is assumed — the premise of this entire effort — every byte the guest emits is attacker-controlled, including response descriptors. Host adapters MUST:

- Treat `AbiResponse.ptr`/`AbiResponse.len` as untrusted: validate `ptr + len` against the current linear-memory size with integer-overflow checks before every read. A descriptor that fails validation poisons the instance.
- Fail closed on malformed payloads: a decode error is a terminal instance error, never a value silently coerced or skipped.
- Bound decode work: enforce the caps in Resource Controls before parsing, cap container nesting depth, and avoid recursive or quadratic parsing strategies.
- Never call back into guest exports from inside a host import. The sync host-call flow depends on this no-reentrancy invariant; it is enforced by a conformance test, not convention.
- Re-validate memory views after growth: `memory.buffer` is detached/replaced when guest memory grows; adapters must re-acquire views rather than caching them across export calls.

Because Python and TypeScript hosts cannot run the shared Rust codec, there are three codec implementations: the guest's Rust codec (the reference) plus the Python and TypeScript host codecs. Divergence between them is a protocol-confusion vulnerability. The test strategy must include differential fuzzing across all three (see Fuzzing).

### Instance Granularity And Trust Domains

The ABI multiplexes multiple runtimes into one WASM instance (`runtime_id` parameters). That is an ergonomic affordance, not an isolation boundary: all runtimes in an instance share one linear memory, so a QuickJS heap-corruption bug triggered by one runtime can read or corrupt sibling runtimes in the same instance.

Rule: **one WASM instance per trust domain.** This is the WASM-plane successor to the existing "one Runtime per trust domain" rule in the threat model.

- Host adapters may pool instances only within a single trust domain.
- Multi-tenant hosts must map tenant -> instance, never tenant -> runtime_id within a shared instance. Tenant -> instance in one process is the default; tenant -> process is the escalation reserved for the runtime-escape axis (see Deployment Profiles), not a baseline multi-tenancy requirement.
- Snapshots restore within the same trust domain they were created in unless their content is independently trusted.
- The default adapter API should make instance-per-`Runtime` the obvious path and require explicit opt-in to share an instance across `Runtime` objects.

### Deployment Profiles

| Profile | Shape | Intended use |
|---|---|---|
| `wasm-inproc` | Host process instantiates `quickjs-core.wasm` directly | **Default REPL hardening target** — semi-trusted code (agent/LLM-generated, internal skills) |
| `wasm-worker-thread` | Host worker thread/Web Worker owns WASM instance | Default JS-host shape (placement rule); responsiveness, not a stronger security boundary |
| `wasm-worker-process` | Separate process/container owns WASM instance | A WASM-runtime escape is in scope, or the host holds secrets the guest must never reach |
| `native-legacy` | Existing native QuickJS path | Migration/baseline only |

Production recommendation:

- **`wasm-inproc` is the default, and that is the point of this effort.** The WASM plane *is* the isolation boundary: it contains an engine memory-safety bug in guest linear memory without a separate process. For the primary target — semi-trusted code that is buggy or accidentally adversarial (the recursion bomb), not an attacker engineering a runtime escape — one boundary in-process is the right trade, and not needing process-per-execution is the improvement over the native path.
- **Multi-tenancy is a topology, not a threat, and is handled in-process by default.** A REPL-execution service serving many customers is multi-tenant by definition — the expected default deployment. Tenant isolation comes from the instance-granularity invariant (*one WASM instance per trust domain*; see Instance Granularity), realized as **tenant → instance**, not tenant → process. Two instances in one process are already isolated by the WASM sandbox — the boundary this whole effort provides — so `wasm-inproc` with one instance per tenant is the intended multi-tenant default. What is forbidden is multiplexing tenants into one instance (shared linear memory), which the invariant already rules out independent of profile.
- **Escalate to `wasm-worker-process` only on the runtime-escape axis**, which is orthogonal to tenant count, decided on two concrete conditions:
  - *WASM-runtime escape is in scope* — you must assume the WASM runtime itself (e.g. a Cranelift/Wasmtime CVE) can be escaped, which would make sibling instances in the same process co-resident again. The process boundary is the second, independently-failing containment layer. This is a risk-tolerance call about runtime-escape, not a function of how many tenants you serve.
  - *Secret co-residency* — the host process holds high-value data the guest must never reach (API keys, other tenants' data) **and** the guest is hostile. Spectre-class speculation can leave a secret-dependent cache footprint regardless of the guest's capabilities, because mis-speculation happens in silicon below the WASM sandbox. Withholding/coarsening the clock and keeping SharedArrayBuffer out of the secret's address space are real mitigations that raise the bar — but they are probabilistic: a JS attacker can synthesize a timer from a SAB counter-thread (and the worker-hosted timeout mechanism *ships* a SAB — see CPU And Timeouts), and clock coarsening is defeated by amplification. The only categorical control is address-space separation: a secret that does not share an address space with hostile speculation cannot be Spectre-leaked from it. Move the guest out, or move the secrets out. (Semi-trusted guests — the default target — do not meet the "hostile" condition; this is a hostile-code concern, not a blanket one.)
- When neither condition holds — including ordinary multi-tenant serving — a separate process per execution buys negligible additional safety at real cost (startup, IPC marshaling, operational complexity), and choosing it by reflex throws away the reason to adopt the WASM plane at all.

## Testing Strategy

### Conformance Corpus

Build a host-neutral conformance suite covering:

- primitive eval,
- arrays/objects/bytes/BigInt/undefined,
- errors and stack records,
- memory limits,
- timeout behavior,
- handle lifecycle,
- cross-context handle rejection,
- sync host callbacks,
- async host callbacks,
- sync eval triggering async host call,
- cancellation absorption,
- host resolver protocol (static and dynamic import, default-resolver relative/bare semantics, resolver miss),
- namespace/alias resolution: same-named files under different namespaces get distinct canonical keys; two edges resolving to one canonical key share one module instance,
- edge-resolution cache semantics (resolver consulted once per unique edge; repeat edges guest-local),
- module cache semantics, including `qrs_module_cache_clear` invalidation and import cycles converging through already-registered keys,
- TypeScript stripping,
- snapshots where supported.

Run the same cases through:

- Python adapter,
- JS adapter in Node,
- the same JS adapter in a headless browser, if browser support is approved.

### Fuzzing

Fuzz targets (guest-side):

- ABI decoder,
- value decoder,
- request envelope decoder,
- module provide payload decoder,
- snapshot decoder,
- host-call settlement protocol,
- handle ID/generation validation.

Fuzz targets (host-side — guest output is attacker-controlled, so the host decoders are first-class attack surface, not plumbing):

- response descriptor validation in every adapter (out-of-bounds `ptr`/`len`, integer overflow, descriptors pointing at the descriptor itself),
- Python value/envelope decoder,
- TypeScript value/envelope decoder.

Differential fuzzing across the three codec implementations — the guest/reference Rust codec (driven directly by a cargo harness, no host adapter needed), Python, and TypeScript: identical input must yield an identical parse or an identical error in all three. Any divergence is a protocol-confusion bug and fails CI.

### Security Tests

- No filesystem/env/network imports by default.
- Host callback registry cannot be invoked with unknown `fn_id`.
- Pending IDs cannot collide or be settled across contexts.
- Module provide settlements cannot target unknown, already-settled, or foreign request IDs.
- Import specifiers and referrer keys reaching the host resolver are treated as untrusted strings (no implicit path/URL interpretation); referrer-based policy is documented as advisory under engine compromise.
- `qrs_module_provide` settlements whose source does not match the already-registered module for that canonical key are rejected with `invalid_request` (guest-enforced resolver determinism).
- Guest cannot forge host handles.
- Malformed response descriptors are rejected (including `ptr + len` overflow and out-of-bounds reads).
- Guest panic/trap tears down runtime cleanly, and the trapped instance is never reused.
- Checkpoint/restore after a trap resumes from the last checkpoint only — no effect of the poisoned eval is observable in the restored instance.
- Host imports never re-enter guest exports (no-reentrancy invariant).
- Every required adapter cap (argument size, response size, module source size, handle count, pending count) rejects at its boundary.
- Adapters refuse a `quickjs-core.wasm` artifact whose hash does not match the pinned value.
- Snapshot restore refuses unauthenticated snapshots when MAC/signature verification is configured.

### Performance Benchmarks

Measure:

- WASM module load/compile time,
- runtime creation,
- context creation,
- sync eval latency,
- async eval latency,
- host callback overhead,
- module import overhead,
- memory overhead per runtime/context,
- large value marshal overhead,
- handle operation overhead.

Keep native QuickJS baseline benchmarks during migration, but do not let native parity block security hardening unless regressions are product-breaking.

## Migration Plan

### Phase 0: Decision Record

Deliverables:

- ADR approving portable WASM-first execution plane.
- The ADR includes a retrospective on the project's earlier v0.2 WASM implementation (removed in `736c528`, benchmarked against native in `ada54b4`): why it was abandoned, what the measured performance deltas were, and why those forces do not sink the migration a second time. The ADR sets an explicit callback-overhead performance budget informed by that data.
- Agreement that PyO3/N-API are not primary architecture.
- Agreement on initial WASM target: likely `wasm32-wasip1`.
- Agreement on minimum supported hosts for V1: Python and Node only. The Rust host adapter is deferred until a Rust consumer exists (see Rust Host Adapter); conformance and fuzzing do not require it.

Exit criteria:

- Architecture owner and security owner agree on the target shape.

### Phase 1: Minimal Guest Core

Note: an initial Phase 1 spike was built (`wasm32-wasip1`, ~1.2 MB artifact) and produced the findings recorded in this spec (sync host-call import flow, epoch interruption, the quickjs-ng wasi stack-limit behavior), but its source was not preserved. Phase 1 is to be re-executed; this document is the canonical record of the spike's conclusions.

Scope:

- Build `quickjs-core.wasm` with QuickJS, bound via `rquickjs` (decision: see Open Questions / Build).
- Expose ABI version, alloc/free, runtime/context create/close, primitive eval, and the `host_interrupt` import channel (graceful timeout tier).
- No host callbacks, modules, handles, or snapshots.

Exit criteria:

- Python adapter can run `1 + 2`.
- Guest-crate unit tests (cargo, no host adapter) cover alloc/free and the version export.
- Runaway allocation raises `MemoryLimitError` (not host OOM) in the Python host.
- A hostile infinite loop (`for(;;);`) is first caught gracefully: the `host_interrupt` import flips at the deadline, the eval returns `TimeoutError`, and the instance survives and accepts further evals (native timeout parity).
- With the graceful channel disabled (simulating an unreachable cooperative check), the same loop is terminated via Wasmtime epoch interruption in the Python (`wasmtime-py`) host, and the trapped instance is discarded — before any callback or handle work begins.
- The Python epoch demonstration explicitly verifies the GIL interaction: the trap must fire while the main Python thread is blocked inside the eval call. If `wasmtime-py` holds the GIL across wasm execution, the watchdog thread can never increment the epoch and the entire Python preemption story is fiction — in that case the Python runtime choice is re-opened. **This is a go/no-go gate for the Python host.**
- The Node interruption story (worker-hosted instance + `SharedArrayBuffer` flag + worker-termination backstop) is documented with its limitations.
- `quickjs-core.wasm` binary size is measured and reviewed against packaging budgets (reference points: v0.2 shipped ~1 MB gzipped; the lost spike artifact was 1.2 MB raw). If rquickjs proves materially heavier than those references, the binding decision is revisited with data.
- The wasi stack-check restoration is investigated with a verdict: either unbounded recursion surfaces as a catchable `InternalError` with a surviving context (trap path closed), or the limitation is confirmed and recursion is documented as trap-reachable.

### Phase 2: Value Protocol And Errors

Scope:

- Implement tagged value codec.
- Implement JS error records.
- Implement primitive/container marshaling.
- Implement BigInt and bytes.

Exit criteria:

- Primitive and container tests pass in the Python host.
- Malformed value payload fuzzing exists (cargo harness for the reference codec; Python host decoder).

### Phase 3: Handles

Scope:

- Guest handle table.
- Handle IDs/generations.
- `get`, `set`, `call`, `call_method`, `new`, `to_python` equivalent.

Exit criteria:

- Current handle tests pass through Python adapter.
- Cross-context and disposed-handle errors are preserved.

### Phase 4: Host Callback State Machine

Scope:

- Sync host function registration.
- Async host function registration.
- Event/poll protocol.
- Promise settlement.
- Cancellation semantics.

Exit criteria:

- Existing sync/async host callback tests pass through Python adapter.
- JS adapter prototype (Node) can run a Promise-backed async callback.

### Phase 5: Modules And TypeScript

Scope:

- `ModuleRequest` poll event and `qrs_module_provide` settlement export (host-side resolution: canonical key + source).
- Edge-resolution cache and module cache inside guest; `qrs_module_cache_clear`.
- Default resolver in the Python and TS adapters (conformance-pinned; preserves current native semantics).
- Static and dynamic import through the host resolver.
- TypeScript stripping strategy.

Exit criteria:

- Existing import-handler module tests pass through the Python adapter (via the default resolver).
- Namespace/alias resolution works: edges under different namespaces resolve to distinct canonical keys; the collision case (relative import inside a bare-keyed module) is covered by a conformance test.
- Dynamic import works for downstream REPL skill loaders.
- TypeScript syntax errors surface when the handler-provided source is loaded, matching current behavior.

### Phase 6: Snapshots

Scope:

- Snapshot create/restore.
- Snapshot compatibility policy.
- Tombstone behavior.

Exit criteria:

- Snapshot tests pass or unsupported cases are explicitly documented.
- Snapshot payload includes version/build metadata.

### Phase 7: Packaging And Defaults

Scope:

- Package `quickjs-core.wasm` with Python wheel/sdist.
- Package `quickjs-core.wasm` with the JS npm package.
- Reproducible build for `quickjs-core.wasm`; the artifact hash is pinned in the Python wheel and npm package and verified at load time, so a swapped artifact is refused.
- Extend the existing CI dependency gates (`cargo audit`, Cargo.lock git-source allowlist) to cover Wasmtime/`wasmtime-py` and the JS adapter toolchain.
- Add headless-browser conformance CI for the JS package if browser support is approved.
- Switch REPL default to WASM execution plane.

Exit criteria:

- REPL uses WASM execution by default.
- Native path, if retained, is explicitly labeled legacy/trusted-performance only.
- Security docs describe `wasm-inproc` vs `wasm-worker-process`.

## Open Questions

### Build

- Resolved (2026-06-12): the guest binding layer is Rust via `rquickjs` (over quickjs-ng), with per-call drop-down to the re-exported raw sys layer (`rquickjs::qjs`) where `rquickjs` lacks an API — the pattern the native snapshot code already uses. No hand-written C shim layer: the v0.2 retrospective (ADR 0001) forecloses it. Rationale: Javy proves rquickjs on `wasm32-wasip1`; the native semantics layer (marshal, modules, errors, handles) ports rather than being rewritten against raw FFI; the WASM sandbox contains binding bugs either way, so sys-everywhere buys only more unsafe surface. Phase 1 exit measures binary size to confirm; the `qjs` drop-down is the escape hatch if rquickjs fights the poll state machine, not a re-architecture.
- Can current QuickJS/quickjs-ng build reliably to `wasm32-wasip1`? (The lost Phase 1 spike's artifact suggests yes; re-confirm in Phase 1.)
- What is the size impact of including TypeScript stripping inside the guest?
- What allocator should the guest use?

### ABI

- Binary codec vs existing format such as MessagePack?
- Raw exports forever, or WIT/component model after V1?
- Should handle operations support batching (multi-get/path-get) to bound boundary-crossing overhead for chatty REPL access patterns? Decide before Phase 3 freezes the handle ABI.
- How do we represent diagnostics without leaking secrets?

### Runtime

- Resolved: `wasmtime-py` is the first Python adapter runtime; epoch interruption is a hard requirement for the Python host (see CPU And Timeouts). Open: do wasmer/wasmedge adapters matter enough to justify abstracting over a Wasmtime-specific interruption mechanism?
- What is the minimum acceptable browser support story, given its weaker (cooperative + worker-termination) interruption model?

### Product

- Is the Python package still named `quickjs-rs` if the primary implementation no longer uses PyO3/Rust native extension semantics?
- Should native QuickJS remain available for trusted high-performance workloads?
- What public API changes are acceptable if exact compatibility is expensive?
- What performance budget is acceptable for callback-heavy REPL workloads?

## Initial Backlog

1. Write ADR: portable `quickjs-core.wasm` is the target REPL execution plane.
2. Create `crates/quickjs-core-abi` with envelope/value/error structs and debug JSON codec.
3. Create minimal `crates/quickjs-core` compiling to WASM with alloc/free and version export.
4. Add guest-crate unit tests driven by cargo (no host adapter).
5. Add Python host smoke test using a Python WASM runtime.
6. Compile QuickJS into the guest and run primitive eval.
7. Add memory-limit and timeout experiments before callbacks.
8. Implement value codec and error records.
9. Implement handle table.
10. Implement poll/event host callback protocol.
11. Port modules and TypeScript stripping.
12. Build the isomorphic JS package (Node + browser) around the same WASM artifact.
13. Add conformance matrix across Python/Node.
14. Update security docs and REPL default configuration.

## References

- Javy — feasibility prior art: QuickJS via `rquickjs` compiled to `wasm32-wasip1`: https://github.com/bytecodealliance/javy
- WebAssembly security model: https://webassembly.org/docs/security/
- Wasmtime security model: https://docs.wasmtime.dev/security.html
- Wasmtime Python bindings: https://github.com/bytecodealliance/wasmtime-py
- Wasmtime Python API docs: https://bytecodealliance.github.io/wasmtime-py/
- Wasmtime interruption mechanisms: https://docs.wasmtime.dev/examples-interrupting-wasm.html
- wasm-bindgen guide: https://rustwasm.github.io/docs/wasm-bindgen/
- wasm-bindgen Node deployment: https://rustwasm.github.io/docs/wasm-bindgen/reference/deployment.html
- Node-API docs: https://nodejs.org/api/n-api.html
- PyO3 guide: https://pyo3.rs/
