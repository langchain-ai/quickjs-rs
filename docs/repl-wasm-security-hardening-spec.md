# REPL WASM Execution Plane Security Hardening Spec

Date: 2026-06-10
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
Rust host API   -> Wasmtime host adapter      -> quickjs-core.wasm
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
Rust             | quickjs Rust host API |
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
  quickjs-host-rust/         # Rust host adapter using Wasmtime directly

python/
  quickjs_rs/                # Python public API over a WASM runtime adapter

js/
  quickjs-wasm/              # isomorphic Node + browser TypeScript package over the standard WebAssembly API
```

There is no separate browser package or browser build artifact. The JS package is isomorphic; environment differences (byte loading, worker backstop) live behind conditional exports. See JS Host Adapter.

Optional later packages:

```text
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

Module source acquisition is host-mediated through the eval poll state machine, mirroring the current native architecture where `runtime.set_import_handler(...)` owns source lookup. The guest owns specifier normalization and the module cache; the host owns source lookup only. See the Module Loading section for the full flow.

```text
qrs_module_provide(eval_id, request_id, flags, source_ptr, source_len, out_response_ptr) -> status
qrs_module_cache_clear(runtime_id, request_ptr, request_len, out_response_ptr) -> status  # optional/future
```

`qrs_module_provide` settles a pending `ModuleRequest` event from `qrs_eval_poll`:

- `flags = source`: bytes are UTF-8 module source for the requested key; the guest registers, optionally type-strips, compiles, and caches it.
- `flags = miss`: the host has no module for this key; the guest raises the import error inside JS.

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
TypeScript source (returned by host import handler)
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
- Manage the callback registry and import handler.
- Map async host functions to Promises.
- Drive the eval poll state machine on the event loop.
- Integrate with timers/`AbortSignal` for cancellation and timeout.
- No N-API and no `wasm-bindgen` in the primary package.

Environment differences are confined to a small conditional-exports seam (`"node"` / `"default"` entry points):

- Byte loading: filesystem read in Node; `fetch`/`instantiateStreaming` in browsers. The API should also accept caller-provided bytes (`BufferSource | Response | URL`) to sidestep loader policy entirely.
- Worker-termination backstop: `worker_threads` in Node, Web Worker in browsers (see CPU And Timeouts).

WASI shim rules:

- The package ships one small pure-JS zero-capability `wasi_snapshot_preview1` shim used in **both** Node and browsers. Do not use `node:wasi`: the guest must see an identical import environment in every JS host.
- With zero ambient capabilities configured, the expected import surface is small: `clock_time_get`, `random_get`, `fd_write` (panic/diagnostic output only), `environ_*` stubs, `proc_exit`.
- Clock and randomness are policy points: the shim must support coarsening or fixing `clock_time_get` and deterministic `random_get` for deployments that want to withhold timing capabilities from guest JS.

Browser is a conformance target, not a package: CI runs the same package's conformance suite in a headless browser. It remains a distinct target because timer, memory, threading, and interruption behavior differ from server hosts (see Resource Controls).

## Rust Host Adapter

A Rust host adapter should exist for testing and server-side embedding:

```text
quickjs-host-rust
  -> Wasmtime Engine/Module/Store
  -> typed wrappers over ABI calls
  -> callback trait registry
  -> async driver
```

This adapter is useful for:

- conformance tests,
- fuzzing,
- benchmarks,
- debugging guest ABI issues,
- non-Python/non-Node consumers.

## Module Loading

`ModuleScope` no longer exists. The current architecture is a runtime-level dynamic import handler, and the WASM design must preserve its semantics:

```text
handler(requested_key, referrer, specifier) -> source | miss
```

- Relative specifiers are normalized against the importing module key inside the engine before the handler is called.
- Bare specifiers are passed to the handler unchanged.
- `referrer` is the importing module key, or none for top-level eval.
- A returned source string registers and loads that module key; a miss raises the import error inside JS.
- Resolved modules are cached; QuickJS module cache semantics remain visible.
- TypeScript keys (`.ts`, `.mts`, `.cts`, `.tsx`) are type-stripped at load.

In the WASM model, module lookup is a host capability expressed through the same event/poll protocol as host callbacks:

```text
1. Guest QuickJS resolver hits an unregistered module key (static or dynamic import).
2. Guest normalizes the specifier against the referrer to produce requested_key.
3. qrs_eval_poll returns ModuleRequest { request_id, requested_key, referrer, specifier }.
4. Host adapter invokes the registered import handler.
5. Host calls qrs_module_provide(eval_id, request_id, source | miss).
6. Guest type-strips if needed, compiles, caches, and resumes evaluation.
7. Host continues qrs_eval_poll.
```

Division of responsibility:

- Guest owns: specifier normalization, module cache, compile/link, TypeScript stripping, import error shape. One resolver implementation across all hosts.
- Host owns: source lookup only. The handler is a capability like any host callback; no handler registered means imports fail.

Consequences to design for:

- Every first import of a key is a host round-trip through the poll loop; repeat imports hit the guest cache. Module-heavy startup cost must be benchmarked (see Performance Benchmarks).
- Module sources are guest-controlled input to the host handler (`specifier` is attacker-chosen text). Host adapters must treat handler arguments as untrusted strings and must not interpret them as filesystem paths or URLs without explicit, validated policy.
- An optional batch/prefetch form (host provides several modules in one settlement) may be added later if round-trip cost shows up in benchmarks; it must not change resolution semantics.

## Resource Controls

### Memory

Controls:

- QuickJS memory limit inside guest.
- WASM linear memory maximum configured by host/runtime.
- Optional host adapter max response size.
- Optional max handle count.
- Optional max module source bytes.

Required tests:

- runaway allocation raises `MemoryLimitError`, not host OOM;
- large ABI response is rejected predictably;
- handle leaks are bounded by configured max handle count.

### CPU And Timeouts

Portable timeout model:

- Host tracks deadlines.
- Guest QuickJS interrupt handler checks a deadline/interrupt flag cooperatively.
- Eval poll loop returns `Timeout` when deadline expires.

Cooperative checks alone cannot stop a hostile `for(;;);` if the guest never yields, so each supported host must have a named preemption mechanism. This is a V1 requirement, not an optional hardening layer:

| Host | Required mechanism | Strength |
|---|---|---|
| Rust (Wasmtime) | Epoch interruption (`Engine::increment_epoch` from a watchdog thread) | Preemptive; traps mid-loop |
| Python (`wasmtime-py`) | Epoch interruption (exposed by wasmtime-py) | Preemptive; traps mid-loop |
| Node | Cooperative QuickJS interrupt flag, plus worker-thread termination as backstop | Weaker; backstop destroys the instance |
| Browser | Cooperative QuickJS interrupt flag, plus Web Worker termination as backstop | Weaker; backstop destroys the instance |

Rules:

- An epoch/fuel trap tears down the eval and surfaces `TimeoutError`; the runtime instance must either recover to a verified-consistent state or be discarded.
- Worker termination is a backstop, not a timeout mechanism: it loses all runtime state. Adapters relying on it must document that a tight-loop timeout destroys the instance.
- Choosing a Python/Rust WASM runtime without epoch-or-equivalent interruption is not acceptable for V1.

Phase 1 must demonstrate the Rust and Python mechanisms against an infinite loop before any callback or handle work begins (see Phase 1 exit criteria). High-risk deployments should still use worker processes/containers regardless.

### Stack

Controls:

- WASM runtime stack guard (e.g. Wasmtime `max_wasm_stack`).
- Tests for recursion/stack overflow classification.

Phase 1 finding: quickjs-ng **disables its internal stack limit on
`__wasi__`** (`update_stack_limit` sets `stack_limit = 0`), so
`JS_SetMaxStackSize` is a no-op inside the guest. On the WASM plane the
runtime stack guard is the enforcement layer, and stack overflow is a
trap that poisons the instance — a documented deviation from native
semantics, where it is a catchable `InternalError` and the context
survives. Hosts classify the trap distinctly (e.g. `StackOverflow`, not
a generic panic). Revisit if upstream enables shadow-stack checking for
wasi targets.

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

### Instance Granularity And Trust Domains

The ABI multiplexes multiple runtimes into one WASM instance (`runtime_id` parameters). That is an ergonomic affordance, not an isolation boundary: all runtimes in an instance share one linear memory, so a QuickJS heap-corruption bug triggered by one runtime can read or corrupt sibling runtimes in the same instance.

Rule: **one WASM instance per trust domain.** This is the WASM-plane successor to the existing "one Runtime per trust domain" rule in the threat model.

- Host adapters may pool instances only within a single trust domain.
- Multi-tenant hosts must map tenant -> instance (or stronger: tenant -> process), never tenant -> runtime_id within a shared instance.
- Snapshots restore within the same trust domain they were created in unless their content is independently trusted.
- The default adapter API should make instance-per-`Runtime` the obvious path and require explicit opt-in to share an instance across `Runtime` objects.

### Deployment Profiles

| Profile | Shape | Intended use |
|---|---|---|
| `wasm-inproc` | Host process instantiates `quickjs-core.wasm` directly | Default REPL hardening target |
| `wasm-worker-thread` | Host worker thread/Web Worker owns WASM instance | UI/server responsiveness; not a strong security boundary |
| `wasm-worker-process` | Separate process/container owns WASM instance | High-risk untrusted code |
| `native-legacy` | Existing native QuickJS path | Migration/baseline only |

Production recommendation:

- Use `wasm-inproc` for semi-trusted REPL workloads.
- Use `wasm-worker-process` for hostile/multi-tenant workloads.
- In every profile, never multiplex runtimes from different trust domains into one WASM instance.

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
- import handler resolution (static and dynamic import, relative normalization, bare specifiers, handler miss),
- module cache semantics,
- TypeScript stripping,
- snapshots where supported.

Run the same cases through:

- Rust host adapter,
- Python adapter,
- JS adapter in Node,
- the same JS adapter in a headless browser, if browser support is approved.

### Fuzzing

Fuzz targets:

- ABI decoder,
- value decoder,
- request envelope decoder,
- module provide payload decoder,
- snapshot decoder,
- host-call settlement protocol,
- handle ID/generation validation.

### Security Tests

- No filesystem/env/network imports by default.
- Host callback registry cannot be invoked with unknown `fn_id`.
- Pending IDs cannot collide or be settled across contexts.
- Module provide settlements cannot target unknown, already-settled, or foreign request IDs.
- Import specifiers reaching the host handler are treated as untrusted strings (no implicit path/URL interpretation).
- Guest cannot forge host handles.
- Malformed response descriptors are rejected.
- Guest panic/trap tears down runtime cleanly.

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
- Agreement that PyO3/N-API are not primary architecture.
- Agreement on initial WASM target: likely `wasm32-wasip1`.
- Agreement on minimum supported hosts for V1: Python and Node, Rust host for tests.

Exit criteria:

- Architecture owner and security owner agree on the target shape.

### Phase 1: Minimal Guest Core

Scope:

- Build `quickjs-core.wasm` with QuickJS.
- Expose ABI version, alloc/free, runtime/context create/close, primitive eval.
- No host callbacks, modules, handles, or snapshots.

Exit criteria:

- Rust host adapter can run `1 + 2`.
- Python adapter can run `1 + 2`.
- Runaway allocation raises `MemoryLimitError` (not host OOM) in both hosts.
- A hostile infinite loop (`for(;;);`) is terminated via Wasmtime epoch interruption in both the Rust and Python (`wasmtime-py`) hosts, surfacing `TimeoutError`, before any callback or handle work begins.
- The Node interruption story (cooperative flag + worker-termination backstop) is documented with its limitations.

### Phase 2: Value Protocol And Errors

Scope:

- Implement tagged value codec.
- Implement JS error records.
- Implement primitive/container marshaling.
- Implement BigInt and bytes.

Exit criteria:

- Primitive and container tests pass in Rust and Python hosts.
- Malformed value payload fuzzing exists.

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

- `ModuleRequest` poll event and `qrs_module_provide` settlement export.
- Specifier normalization and module cache inside guest.
- Static and dynamic import through the host import handler.
- TypeScript stripping strategy.

Exit criteria:

- Existing import-handler module tests pass through the Python adapter.
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
- Add headless-browser conformance CI for the JS package if browser support is approved.
- Switch REPL default to WASM execution plane.

Exit criteria:

- REPL uses WASM execution by default.
- Native path, if retained, is explicitly labeled legacy/trusted-performance only.
- Security docs describe `wasm-inproc` vs `wasm-worker-process`.

## Open Questions

### Build

- Can current QuickJS/quickjs-ng build reliably to `wasm32-wasip1`?
- Can we use `rquickjs` inside the guest, or do we need a smaller raw QuickJS wrapper?
- What is the size impact of including TypeScript stripping inside the guest?
- What allocator should the guest use?

### ABI

- Binary codec vs existing format such as MessagePack?
- Raw exports forever, or WIT/component model after V1?
- Should handle operations support batching (multi-get/path-get) to bound boundary-crossing overhead for chatty REPL access patterns? Decide before Phase 3 freezes the handle ABI.
- How do we represent diagnostics without leaking secrets?

### Runtime

- Resolved: `wasmtime-py` is the first Python adapter runtime; epoch interruption is a hard requirement for Python/Rust hosts (see CPU And Timeouts). Open: do wasmer/wasmedge adapters matter enough to justify abstracting over a Wasmtime-specific interruption mechanism?
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
4. Add Rust host smoke test using Wasmtime.
5. Add Python host smoke test using a Python WASM runtime.
6. Compile QuickJS into the guest and run primitive eval.
7. Add memory-limit and timeout experiments before callbacks.
8. Implement value codec and error records.
9. Implement handle table.
10. Implement poll/event host callback protocol.
11. Port modules and TypeScript stripping.
12. Build the isomorphic JS package (Node + browser) around the same WASM artifact.
13. Add conformance matrix across Rust/Python/Node.
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
