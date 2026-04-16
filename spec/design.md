# quickjs-py: sandboxed JS in Python, design spec

## Why

deepagents has interpreter middleware in JS via `@langchain/quickjs`. The Python side (langgraph-py) has no comparable story. Agents that need to sandbox model-generated JS — or any string-of-code tool — from a Python host currently reach for `subprocess` + node, `py-mini-racer` (V8-sized, heavy), or `pythonmonkey` (SpiderMonkey, large binary, heavy native deps). QuickJS is small, embeddable, and already battle-tested in our JS stack. A WASI build hosted from Python gives us the same guarantees the JS middleware relies on, minus the Emscripten glue that makes `quickjs-emscripten` Python-hostile.

**Non-goals**: Node.js compatibility, `fs`/`net` access by default, debugger support in v1, running existing JS agent graphs from Python.

## Shape

Three layers:

1. **Wasm module** — QuickJS built with WASI-SDK plus a small C shim that re-exports the parts of the QuickJS C API we need as wasm exports, and declares imports for host callbacks. Single architecture-independent `quickjs.wasm`, ~1 MB gzipped.
2. **Python bridge** — `quickjs_py._bridge`, thin wrapper over `wasmtime-py`. Loads the module, wires WASI stubs (deny-by-default: no clock, no FS, no env, no stdio), owns guest-side allocation helpers, dispatches host calls into a function registry.
3. **Python API** — `quickjs_py`, user-facing. `Runtime`, `Context`, `Handle`, plus helpers.

Package: TBD name (see open questions). Pure-Python wheel, zero native deps besides `wasmtime-py` which ships its own prebuilt wheels. One wheel, all platforms.

## Why wasmtime-py, why a custom shim

`wasmtime-py` is the best-maintained Python wasm runtime right now. `wasmer-python` is effectively abandoned. `wasmer` the CLI is still alive but the Python bindings aren't. Wasmtime gives us epoch-based interruption, fuel metering, and clean component-model support if we want it later.

On the shim: we could in theory use `javy` (Shopify's "JS → wasm component" tool), but javy is opinionated toward a stdin/stdout contract for running JS functions as serverless handlers. For an interpreter middleware we need direct access to contexts, globals, host-defined functions, interrupt handlers — the C API. The shim is maybe 500 lines of C and gives us full control.

## API

Mirrors `quickjs-emscripten` where it makes sense, Pythonic where it doesn't.

```python
from quickjs_py import Runtime

with Runtime() as rt:
    with rt.new_context(memory_limit=64 * 1024 * 1024, timeout=5.0) as ctx:
        # eval with auto-marshaling
        ctx.eval("1 + 2")                          # -> 3
        ctx.eval('({a: 1, b: [2, 3]})')            # -> {"a": 1, "b": [2, 3]}

        # globals
        ctx.globals["name"] = "world"
        ctx.eval('`hello ${name}`')                # -> "hello world"

        # register a Python function as JS-callable
        @ctx.function
        def add(a: int, b: int) -> int:
            return a + b
        ctx.eval("add(1, 2)")                      # -> 3

        # handles for values kept across calls
        with ctx.eval_handle("({x: 1, y: 2})") as obj:
            obj.get("x").to_python()               # 1
            obj.call("toString")                   # handle to a string
```

**Runtime** — owns the wasm instance. Cheap to create; no reason to have more than one per process unless you want isolated memory-limit domains.

**Context** — a QuickJS `JSContext`. Where code actually runs. Multiple per Runtime.

**Handle** — opaque wrapper around a `JSValue` in guest memory. Context managers work; explicit `.dispose()` also fine. `__del__` disposes as a safety net and emits a `ResourceWarning` (Python convention for leaked resources).

## Value marshaling

Two paths:

- `ctx.eval(code)` — run, convert result to Python via MessagePack on the guest side, free the `JSValue`. Fast path for ~95% of uses.
- `ctx.eval_handle(code)` — run, return a `Handle`. Traverse with `.get(k)`, `.call(method, *args)`, `.to_python()`. Required when the value has functions, circular refs, or you want to hold it across many calls without re-marshaling each time.

Primitives pass natively through the ABI. Objects/arrays marshal through MessagePack — not JSON — because it preserves the int/float distinction, handles bytes, and is ~3x faster to decode in Python. `Handle.to_python()` raises on non-serializable values (functions, symbols) unless you pass `allow_opaque=True`, which keeps them as child handles.

## Host functions

```python
@ctx.function
def fetch(url: str) -> str:
    return httpx.get(url).text

ctx.register("sleep", async_sleep_fn, is_async=True)
```

Each registered function gets an integer ID in a host-side registry. The shim exports `qjs_register_host_fn(ctx, fn_id, name_ptr, name_len)` which creates a JS function that, on call, packs its arguments into a scratch buffer and invokes the host import `env.host_call(fn_id, args_ptr, args_len, out_ptr, out_len)`. Python reads args with MessagePack, invokes the function, writes the MessagePack-encoded result back into guest memory through our allocator, returns.

**Async host functions**: the JS side gets a Promise. We create it with `JS_NewPromiseCapability`, hold the resolve/reject handles in a host-side map keyed by promise ID, return the promise synchronously. When the Python coroutine completes, we call the appropriate resolver and then drain pending jobs. User code should use `eval_async` or drive `execute_pending_jobs()` manually.

## Limits and interruption

- `memory_limit` (bytes) → `JS_SetMemoryLimit`
- `stack_limit` (bytes) → `JS_SetMaxStackSize`
- `timeout` (seconds) → installed as a `JS_SetInterruptHandler` that checks wall clock via the host
- `opcode_budget` (int, optional) → deterministic limit using the same interrupt handler, for tests/CI
- Wasmtime epoch interruption is enabled and bound to the same deadline as a belt-and-suspenders for code paths that somehow escape the QuickJS interrupt (e.g. an infinite C loop, which shouldn't happen in well-formed QuickJS but let's not bet on it)

Defaults are strict: 64 MB memory, 1 MB stack, 5 s timeout. Users override per context.

## Async and jobs

```python
result = await ctx.eval_async("fetch('...').then(r => r.text())")
```

`eval_async`:
1. `qjs_eval` the code
2. If the result is a pending promise, loop: drain pending jobs, yield to the Python event loop so any host async futures can make progress, check promise state
3. Return resolved value or raise rejected value as `JSError`

Without asyncio: `ctx.eval(...)` runs sync jobs to completion. If a job is genuinely async (awaiting a Python future), it raises `SyncEvalWithAsyncJob` so the user knows to switch to `eval_async`.

## Errors

`JSError extends Exception`. Carries `name`, `message`, `stack` from the JS side. `ctx.eval("throw new TypeError('x')")` raises `JSError(name='TypeError', message='x', stack=...)`. Python exceptions raised inside a `@ctx.function` are caught, wrapped as a JS `HostError`, and propagated into JS where user code can `catch` them. If they escape back out, they're re-raised in Python with the original exception chained as `__cause__`.

## Shim ABI (sketch)

Exports the C shim provides to the host:

```
qjs_new_runtime() -> u32
qjs_free_runtime(rt)
qjs_new_context(rt) -> u32
qjs_free_context(ctx)
qjs_eval(ctx, code_ptr, code_len, flags, out_val_ptr) -> i32
qjs_get_global(ctx, out_val_ptr)
qjs_get_prop_str(ctx, obj_ptr, key_ptr, key_len, out_val_ptr) -> i32
qjs_set_prop_str(ctx, obj_ptr, key_ptr, key_len, val_ptr) -> i32
qjs_call(ctx, fn_ptr, this_ptr, argc, argv_ptr, out_val_ptr) -> i32
qjs_free_value(ctx, val_ptr)
qjs_dump_msgpack(ctx, val_ptr, out_buf_ptr, out_len_ptr) -> i32
qjs_is_exception(val_ptr) -> i32
qjs_get_exception(ctx, out_val_ptr)
qjs_execute_pending_job(rt) -> i32    // 0=none, 1=ran, -1=exception
qjs_set_memory_limit(rt, bytes)
qjs_set_max_stack_size(rt, bytes)
qjs_set_interrupt_handler(rt)
qjs_register_host_fn(ctx, fn_id, name_ptr, name_len, out_val_ptr) -> i32
qjs_malloc(size) -> u32
qjs_free(ptr)
```

Imports the host provides:

```
env.host_call(fn_id, args_ptr, args_len, out_ptr, out_len) -> i32
env.host_interrupt() -> i32    // non-zero to interrupt
env.host_log(level, ptr, len)  // internal diagnostics, not exposed to JS
```

QuickJS's `JSValue` on wasm32 is a 16-byte struct (tag + union), so we pass pointers through out-params rather than trying to pack into return values. Slightly more overhead than NaN-boxed returns but simpler and the overhead is negligible next to interpretation cost.

## Build pipeline

```
vendor/quickjs-ng/     # pinned submodule
src/shim.c             # the ABI above
src/shim.h
CMakeLists.txt         # uses WASI-SDK toolchain file
build.sh               # cmake -> wasm-opt -O3 -> strip -> quickjs.wasm
```

Build in CI with WASI-SDK 24+ pinned. Output reproducible — same story as the supply-chain work on the JS side; we want to be able to attest what's in the wheel. Ship the `.wasm` as a package resource. Wheel is `py3-none-any`; wasmtime-py provides the native bits.

## Phasing

**v0.1** — sync `eval`, globals, handles, primitives + object/array marshaling, memory/timeout limits, `JSError`. Enough to write a first-cut langchain interpreter middleware. ~2 weeks.

**v0.2** — sync host functions, MessagePack marshaling, proper handle lifecycle with `ResourceWarning`, opcode budget. ~1 week.

**v0.3** — async: `eval_async`, async host functions, job queue integration with asyncio. ~1–2 weeks.

**v0.4** — module loader hook (guest-side `import`), large-payload fast path (avoid round-trip msgpack for big byte blobs), `ptc: true` parity semantics where applicable. ~1 week.

**v1.0** — `langchain-quickjs` Python middleware package on top, mirroring `@langchain/quickjs` semantics.

## Open questions

- **Upstream vs quickjs-ng**: ng has better maintenance and WASI fixes; upstream is canonical. Lean ng unless there's a concrete reason not to.
- **Fuel vs epoch vs QuickJS interrupt**: all three work at different granularities. Start with QuickJS interrupt + wasmtime epoch; add fuel as an opt-in for deterministic testing.
- **Component model**: probably not for v1 — raw WASI + custom imports is simpler and the component toolchain for embedding C projects isn't fully there yet. Revisit for v2.
- **Runtime-per-context default**: `JSRuntime` is the memory-limit boundary; `JSContext` shares the runtime's heap. One-runtime-per-context is safer for per-agent isolation but more memory. Make it configurable, default to one-per-context for agent workloads.
- **Naming**: `quickjs` is taken on PyPI (Petter Strandmark's binding to native QuickJS via C extension — active but a different thing). Need a name that doesn't confuse. `quickjs-wasm`, `wasmquickjs`, `pyquickjs`, or just roll with `langchain-quickjs-py` and skip the standalone library for now.
- **Do we even need the standalone library?** If the only consumer is `langchain-quickjs-py`, maybe skip the split. Counter-argument: `quickjs-emscripten` exists independently of `@langchain/quickjs` and that's healthy — people use it for non-agent work and it gets more eyes on the bridge code than a LangChain-scoped package would. I'd keep them separate.
