# quickjs-rs rewrite spec

Version: 0.3.0 target
Status: planning
Predecessor: `quickjs-wasm` v0.2 (complete, rc1 soaking)

## 1. What and why

`quickjs-wasm` currently executes JavaScript via a five-layer stack:

```
Python code → quickjs_wasm (Python) → wasmtime-py (PyO3) → wasmtime (Rust JIT) → quickjs.wasm → QuickJS (C)
```

This works, but the costs are real: 1-second cold start from wasm JIT compilation, ~110 µs eval floor from four boundary crossings, 1800 lines of hand-written C shim with manual refcounting and msgpack encoding, and a build pipeline requiring WASI-SDK + CMake + wasm-opt.

The rewrite collapses this to:

```
Python code → quickjs_rs (PyO3 native extension) → rquickjs (Rust) → QuickJS (C, compiled in)
```

Two layers. One language boundary (Python → native). Zero wasm. The Python API is unchanged — `Runtime`, `Context`, `Handle`, `eval`, `eval_async`, `@ctx.function`, all the same types, all the same semantics. The test suite transfers verbatim. The benchmarks transfer and should improve dramatically.

**Name**: `quickjs-rs` (PyPI) / `quickjs_rs` (import). Renaming from `quickjs-wasm` because the wasm layer is gone and the name would be actively misleading. The rename happens in-place in the same repo — one commit renames the Python package directory (`quickjs_wasm/` → `quickjs_rs/`), updates `pyproject.toml`, and find-replaces imports across tests and benchmarks. The git history from the wasm era is preserved and useful for understanding design decisions.

**License**: MIT, matching QuickJS and rquickjs upstream.

**Python version**: 3.11+. Unchanged from v0.2.

## 2. What we lose, what we gain, what stays the same

### Loses

- **Wasm sandbox.** QuickJS runs in-process. A hypothetical QuickJS interpreter bug triggered by adversarial JS could affect the host process. Mitigated by: QuickJS exposes no system APIs by default (no fs, no net, no process), agent-generated JS is typically simple, quickjs-ng is actively maintained with security fixes.
- **Architecture-independent wheel.** Wheels become platform-specific (`manylinux`, macOS x86/arm, Windows). Maturin + CI handles this; pydantic/polars/ruff all ship the same way.
- **Component-model migration path.** The WIT file becomes archival documentation rather than a future migration target. If component-model portability is needed later, rquickjs compiles to wasm just as well — the Rust code transfers.

### Gains

- **~1000x faster cold start.** dlopen vs wasm JIT compilation. ~1 ms vs ~1 s.
- **~10x faster eval floor.** Single FFI crossing vs four boundary crossings. Target: 5-15 µs vs 110 µs.
- **Zero C maintenance.** The 1800-line C shim goes away entirely. rquickjs owns the QuickJS binding — refcounting, memory lifecycle, type conversions, async job queue.
- **Standard build toolchain.** Maturin (same as pydantic, polars, ruff). `maturin develop` for local builds, `maturin build` for wheels. No WASI-SDK, no CMake, no wasm-opt.
- **Simpler debugging.** Python → Rust, not Python → Rust → wasm → C. One fewer layer in every stack trace.
- **Smaller dependency tree.** No wasmtime-py (~50 MB installed), no msgpack (canonical ABI replacement not needed; rquickjs handles marshaling natively).

### Stays the same

- **Public Python API.** Every class, method, property, error type, and behavioral contract from §7.2 of the v0.2 spec. The rewrite is invisible to users except for the import path change and performance improvement.
- **Test suite.** All tests from `tests/` transfer with import-path changes only. §13.1 and §13.2 acceptance tests are the north star, unchanged.
- **Benchmark suite.** All benchmarks from `benchmarks/` transfer. Expected numbers change (dramatically better); the benchmark structure doesn't.
- **Spec-driven development process.** CLAUDE.md, spec-conformance tripwires, integration-tests-are-load-bearing, commit discipline — all carry forward.

## 3. Architecture

Two layers:

1. **`quickjs_rs._engine`** — PyO3 Rust extension. Wraps rquickjs with a Python-facing API that mirrors the existing `_bridge.py` contract. Compiled into a platform-specific `.so` / `.dylib` / `.pyd` that ships in the wheel. This is the only native code — there is no separate build step, no vendored `.wasm`, no shim.

2. **`quickjs_rs`** (top-level) — User-facing Python API. `Runtime`, `Context`, `Handle`, error types, value marshaling helpers. Thin layer over `_engine`, handling Pythonic patterns (context managers, decorators, asyncio integration) that are easier to express in Python than in PyO3 macros.

```
quickjs_rs/
  __init__.py         # Re-exports
  _engine.so          # PyO3 compiled extension (rquickjs + QuickJS inside)
  runtime.py          # Runtime (thin wrapper over _engine.QjsRuntime)
  context.py          # Context + eval_async driving loop
  handle.py           # Handle
  globals.py          # Globals proxy
  errors.py           # Exception hierarchy (pure Python, unchanged)
```

The split between `_engine` (Rust/PyO3) and the Python layer is deliberate. The Rust side handles: creating/destroying QuickJS runtimes and contexts, eval, property access, function registration, value marshaling between Python types and JS types, interrupt handling. The Python side handles: context managers, `@ctx.function` decorator with `inspect.iscoroutinefunction`, the async driving loop, asyncio task group management, cumulative timeout tracking, error classification (InterruptError vs TimeoutError vs MemoryLimitError from QuickJS exception strings). This split means the Rust code is a stateless bridge — all the async state machine logic stays in Python where it's easier to write, test, and debug.

## 4. Repository layout

After the rewrite, the repo looks like:

```
quickjs-rs/                         # (was quickjs-wasm/)
├── README.md
├── LICENSE
├── pyproject.toml                  # maturin build backend (was setuptools)
├── Cargo.toml                      # NEW: Rust workspace
├── Cargo.lock                      # NEW
├── spec/
│   ├── implementation.md           # This document (replaces the v0.2 spec)
│   ├── design.md                   # Original design rationale (archival)
│   ├── benchmarks.md               # Benchmark spec (unchanged)
│   └── quickjs.wit                 # Archival — original wasm interface contract
├── src/
│   └── lib.rs                      # NEW: PyO3 extension — the entire Rust layer
├── quickjs_rs/                     # (was quickjs_wasm/)
│   ├── __init__.py
│   ├── runtime.py
│   ├── context.py
│   ├── handle.py
│   ├── globals.py
│   ├── errors.py
│   └── py.typed
├── tests/                          # Unchanged (imports updated)
│   ├── conftest.py
│   ├── test_primitives.py
│   ├── test_objects.py
│   ├── test_host_functions.py
│   ├── test_exceptions.py
│   ├── test_limits.py
│   ├── test_handles.py
│   ├── test_globals.py
│   ├── test_async.py
│   ├── test_async_host_functions.py
│   └── test_smoke.py
├── benchmarks/                     # Unchanged (imports updated)
│   ├── conftest.py
│   ├── test_startup.py
│   ├── test_eval_sync.py
│   ├── test_eval_async.py
│   ├── test_marshaling.py
│   ├── test_host_functions.py
│   └── README.md
├── .github/
│   └── workflows/
│       ├── test.yml                # Updated for maturin
│       ├── benchmarks.yml          # Updated for maturin
│       └── release.yml             # Replaced: maturin-action for multi-platform wheels
└── CLAUDE.md
```

What's removed:

- `vendor/quickjs-ng/` — git submodule removed. rquickjs vendors QuickJS internally.
- `wasm/` — entire directory (shim.c, shim.h, CMakeLists.txt, build.sh, wasi-sdk.cmake).
- `quickjs_wasm/_bridge.py` — replaced by `src/lib.rs`.
- `quickjs_wasm/_msgpack.py` — gone. rquickjs + PyO3 handle marshaling natively.
- `quickjs_wasm/_resources/quickjs.wasm` — gone.
- `quickjs_wasm/_resources/` — directory removed.
- `scripts/install-wasi-sdk.sh`, `scripts/verify-reproducible.sh`, `scripts/update-quickjs.sh` — gone.
- `.github/workflows/build-wasm.yml` — gone.

## 5. Toolchain and build

### 5.1 Pinned versions

| Dependency | Version | Notes |
|---|---|---|
| Rust | stable (1.75+) | MSRV for PyO3 0.28+ |
| rquickjs | 0.11+ | Binds quickjs-ng; features: `classes`, `properties`, `futures`, `parallel` |
| PyO3 | 0.28+ | Python↔Rust FFI |
| maturin | 1.5+ | Build backend for PyO3 projects |
| Python | 3.11+ | Unchanged from v0.2 |
| pytest | 8+ | |
| pytest-asyncio | 0.23+ | |
| pytest-codspeed | 3.0+ | Benchmarks |

### 5.2 Build commands

```bash
# Development build (compiles Rust, installs as editable Python package)
pip install maturin
maturin develop --release

# Or with extras
maturin develop --release -E dev

# Run tests
pytest

# Run benchmarks
pytest benchmarks/ --codspeed

# Build release wheel for current platform
maturin build --release

# Build wheels for all platforms (CI only, via maturin-action)
# See .github/workflows/release.yml
```

No submodule init. No WASI-SDK install. No separate wasm build step. `maturin develop` handles everything — downloads rquickjs (which internally fetches and compiles quickjs-ng via `rquickjs-sys`), compiles the PyO3 extension, installs it in the current virtualenv.

### 5.3 Cargo.toml

```toml
[package]
name = "quickjs-rs-python"
version = "0.3.0"
edition = "2021"
rust-version = "1.75"

[lib]
name = "_engine"
crate-type = ["cdylib"]

[dependencies]
pyo3 = { version = "0.28", features = ["extension-module"] }
rquickjs = { version = "0.11", features = [
    "classes",
    "properties",
    "futures",
    "bindgen",       # cross-platform bindings generation
    # NOT "rust-alloc": with rust-alloc enabled, allocations bypass
    # QuickJS's internal accounting and Runtime::set_memory_limit
    # silently becomes a no-op (see rquickjs-core/src/runtime/raw.rs:
    # "Noop when a custom allocator is being used"). The §9 64MB
    # memory-limit invariant is load-bearing — test_memory_limit_*
    # depends on it — so we stay on libc malloc where
    # JS_SetMemoryLimit actually works.
] }

[profile.release]
lto = true           # link-time optimization for smaller, faster binary
codegen-units = 1    # better optimization at cost of compile time
strip = true         # strip debug symbols from release builds
```

### 5.4 pyproject.toml

```toml
[build-system]
requires = ["maturin>=1.5,<2"]
build-backend = "maturin"

[project]
name = "quickjs-rs"
version = "0.3.0"
requires-python = ">=3.11"
# No runtime dependencies — the PyO3 extension is self-contained

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov",
    "pytest-asyncio>=0.23",
    "ruff",
    "mypy",
]
bench = [
    "pytest-codspeed>=3.0",
]

[tool.maturin]
# Mixed-layout: the Python package sits at ./quickjs_rs/ and maturin
# compiles the native extension into quickjs_rs/_engine.<ext>. The
# python-source field is the directory *containing* the package, not
# the package itself.
python-source = "."
module-name = "quickjs_rs._engine"
features = ["pyo3/extension-module"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

**Zero runtime dependencies.** The PyO3 extension bundles everything (rquickjs, QuickJS) into a single `.so`. No wasmtime-py, no msgpack. The wheel is larger (~5-10 MB per platform vs ~2 MB for the pure-Python+wasm wheel) but has zero install-time dependencies beyond Python itself.

## 6. Rust extension specification (src/lib.rs)

The Rust extension exposes a small set of Python-visible types and functions via PyO3. The design principle: **expose the minimum needed for the Python layer to implement the full §7.2 API.** Convenience, decoration, asyncio integration, error classification — all stay in Python.

### 6.1 Exposed types

```rust
#[pyclass]
struct QjsRuntime {
    // owns rquickjs::Runtime
    // configures memory limit, stack limit, interrupt handler
}

#[pyclass]
struct QjsContext {
    // owns rquickjs::Context (or AsyncContext)
    // eval, eval_handle, get/set globals, register host functions
}

#[pyclass]
struct QjsHandle {
    // wraps rquickjs::Persistent<rquickjs::Value>
    // get_prop, set_prop, call, call_method, new_instance, to_python, type_of
}
```

### 6.2 QjsRuntime methods

```rust
#[pymethods]
impl QjsRuntime {
    #[new]
    #[pyo3(signature = (*, memory_limit=None, stack_limit=None))]
    fn new(memory_limit: Option<usize>, stack_limit: Option<usize>) -> PyResult<Self>;

    fn new_context(&self) -> PyResult<QjsContext>;

    fn run_pending_jobs(&self) -> PyResult<u32>;

    fn has_pending_jobs(&self) -> bool;

    /// Install a Python callable as the interrupt handler.
    /// Called periodically by QuickJS during execution.
    /// Return True from the callable to abort execution.
    fn set_interrupt_handler(&self, handler: PyObject) -> PyResult<()>;

    fn close(&mut self) -> PyResult<()>;
}
```

### 6.3 QjsContext methods

```rust
#[pymethods]
impl QjsContext {
    /// Evaluate JS code, return result as a Python value.
    /// Marshaling: JS null→None, bool→bool, number→float/int,
    /// string→str, Uint8Array→bytes, Array→list, Object→dict.
    /// BigInt→int (Python has arbitrary precision).
    /// Functions/symbols raise MarshalError (use eval_handle).
    #[pyo3(signature = (code, *, flags=0, filename="<eval>"))]
    fn eval(&self, code: &str, flags: u32, filename: &str) -> PyResult<PyObject>;

    /// Evaluate JS code, return result as a QjsHandle.
    #[pyo3(signature = (code, *, flags=0, filename="<eval>"))]
    fn eval_handle(&self, code: &str, flags: u32, filename: &str) -> PyResult<QjsHandle>;

    /// Get the global object as a handle.
    fn global_object(&self) -> PyResult<QjsHandle>;

    /// Register a Python callable as a JS global function.
    /// is_async: if true, the JS function returns a Promise.
    /// The callable receives Python values (already marshaled from JS)
    /// and returns a Python value (marshaled back to JS).
    #[pyo3(signature = (name, fn_id, is_async=false))]
    fn register_host_function(&self, name: &str, fn_id: u32, is_async: bool) -> PyResult<()>;

    /// Resolve an async host call's promise with a value.
    fn resolve_pending(&self, pending_id: u32, value: PyObject) -> PyResult<()>;

    /// Reject an async host call's promise with an error.
    fn reject_pending(&self, pending_id: u32, name: &str, message: &str) -> PyResult<()>;

    /// Check promise state: 0=pending, 1=fulfilled, 2=rejected.
    fn promise_state(&self, handle: &QjsHandle) -> PyResult<i32>;

    /// Get promise result (resolved value or rejection reason) as handle.
    fn promise_result(&self, handle: &QjsHandle) -> PyResult<QjsHandle>;

    fn close(&mut self) -> PyResult<()>;
}
```

### 6.4 QjsHandle methods

```rust
#[pymethods]
impl QjsHandle {
    fn get_prop(&self, key: &str) -> PyResult<QjsHandle>;
    fn get_prop_index(&self, index: u32) -> PyResult<QjsHandle>;
    fn set_prop(&self, key: &str, value: &QjsHandle) -> PyResult<()>;

    /// Call this handle as a function.
    fn call(&self, this: Option<&QjsHandle>, args: Vec<&QjsHandle>) -> PyResult<QjsHandle>;
    fn call_method(&self, name: &str, args: Vec<&QjsHandle>) -> PyResult<QjsHandle>;
    fn new_instance(&self, args: Vec<&QjsHandle>) -> PyResult<QjsHandle>;

    /// Marshal to Python value. Returns PyObject.
    #[pyo3(signature = (*, allow_opaque=false))]
    fn to_python(&self, allow_opaque: bool) -> PyResult<PyObject>;

    /// Structural type tag.
    fn type_of(&self) -> String;

    fn is_promise(&self) -> bool;

    /// Create a duplicate handle (increments refcount).
    fn dup(&self) -> PyResult<QjsHandle>;

    fn dispose(&mut self) -> PyResult<()>;

    fn is_disposed(&self) -> bool;
}
```

### 6.5 Host function dispatch

The Rust side doesn't store Python callables directly — that would require complex GIL management inside rquickjs callbacks. Instead, the pattern from the wasm architecture is preserved:

1. Python registers a function with an integer `fn_id` via `register_host_function`.
2. The Rust side creates a JS function that, when called, marshals arguments to Python types, releases the GIL, acquires it again, and calls a Python-side dispatcher with `(fn_id, args)`.
3. The Python-side dispatcher (`Context._dispatch_host_call`) looks up `fn_id` in its registry and calls the actual callable.

This is the same fn_id-and-registry pattern as the wasm version, just without the msgpack encoding step. rquickjs's `FromJs`/`IntoJs` traits handle the JS→Rust conversion; PyO3 handles the Rust→Python conversion. Two conversions instead of four (JS→msgpack→Python→msgpack→JS).

For async host functions: the Rust side creates a JS Promise and returns a `pending_id`. The Python driving loop (`eval_async`) schedules an asyncio task, and on completion calls `resolve_pending` or `reject_pending` through the extension. Same architecture as v0.2, without the wasm indirection.

### 6.6 Marshaling

No more msgpack. Value conversion happens in two steps:

**JS → Python (in Rust via rquickjs + PyO3):**

| JS type | rquickjs Rust type | PyO3 Python type |
|---|---|---|
| null | `Value::Null` | `py.None()` |
| undefined | `Value::Undefined` | `py.None()` (or `Undefined` sentinel) |
| boolean | `bool` | `PyBool` |
| number | `f64` | `PyFloat` (or `PyInt` if integer-valued) |
| bigint | `BigInt` | `PyInt` (Python int is arbitrary precision) |
| string | `rquickjs::String` | `PyString` |
| Uint8Array | `TypedArray<u8>` | `PyBytes` |
| Array | `rquickjs::Array` | `PyList` (recursive) |
| Object | `rquickjs::Object` | `PyDict` (recursive, str keys) |
| function/symbol | — | `MarshalError` (or `QjsHandle` under `allow_opaque`) |

**Python → JS (in Rust via PyO3 + rquickjs):**

| Python type | Rust extraction | JS type |
|---|---|---|
| `None` | `Option<T>::None` | `null` |
| `bool` | `bool` | `boolean` |
| `int` (fits f64) | `i64` → `f64` | `number` |
| `int` (large) | `BigInt` via string | `bigint` |
| `float` | `f64` | `number` |
| `str` | `&str` | `string` |
| `bytes` | `&[u8]` | `Uint8Array` |
| `list`/`tuple` | `Vec<PyObject>` | `Array` (recursive) |
| `dict` (str keys) | `HashMap<String, PyObject>` | `Object` (recursive) |

The `Undefined` sentinel is preserved: `quickjs_rs.Undefined` on the Python side → `Value::Undefined` on the Rust side → JS `undefined`. `ctx.preserve_undefined` controls whether `undefined` maps to `None` or `Undefined` on the way out.

### 6.7 Invariants the Rust extension enforces

- All `QjsHandle` methods validate that the handle is not disposed; raise `InvalidHandleError` on use-after-dispose.
- Handles are bound to their creating context. Cross-context use raises `InvalidHandleError`. Enforced by comparing context pointers.
- `QjsHandle.__del__` calls dispose and emits `ResourceWarning` if not already disposed. **rquickjs's `Persistent<Value>` has no `Drop` impl** — it holds a `Value` with a `'static`-lifetime lie, and `Value::drop` needs a live `Ctx` to call `JS_FreeValue`. Our `QjsHandle::dispose` restores the persistent inside `Context::with(|ctx| { let _ = persistent.restore(&ctx); })` so the restored `Value`'s drop runs while the Ctx is live. A fallback `Drop for QjsHandle` does the same if Python GC's the handle without an explicit dispose. Forgetting this path leaks the JS ref and trips QuickJS's `list_empty(&rt->gc_obj_list)` assertion at `JS_FreeRuntime` time. See step 4 in §15.
- rquickjs manages QuickJS's `JSValue` refcounting via Rust's ownership system. No manual `JS_DupValue` / `JS_FreeValue` calls in our code.
- The interrupt handler is called with the GIL held (necessary to call into Python). QuickJS polls it periodically during bytecode execution; the Rust wrapper uses `Python::attach` (PyO3 0.28 replacement for `with_gil`) to reacquire and call the Python handler. The Python handler reads a wall-clock deadline written by `Context.eval` before each eval and returns true once the deadline has elapsed — see §7.3.
- **v0.3 does not release the GIL during eval.** `eval` and `eval_handle` run with the GIL held throughout, including inside the rquickjs closure and during host-function callbacks. Trade-off: other Python threads are blocked while JS runs. Gain: host-function trampolines and the interrupt handler already have the GIL, no reacquisition dance. §16 flags GIL release as a v0.4 optimization. A consequence: `Python::attach` calls inside rquickjs callbacks (interrupt handler, host-fn trampoline) are cheap reentrant lookups, not lock acquisitions.
- **Reentrant eval from host functions uses an `active_ctx` slot, not nested `Context::with`.** rquickjs's `Context::with(|ctx| ...)` locks the runtime's `safe_ref::Mut` — a non-reentrant `RefCell` without the `parallel` feature. A host function that calls `ctx.eval(...)` from Python would re-enter `Context::with` and panic with "RefCell already borrowed". The extension has a `QjsContext::active_ctx: Cell<Option<Ctx<'static>>>` slot that's populated on entry to `Context::with` and cleared on exit via an RAII guard; the `with_active_ctx` helper checks the slot first and, if populated, runs the closure against the stashed `Ctx` directly (lifetime laundered back from `'static` to the local borrow — same pattern as `rquickjs::Persistent`, scoped to a single call). This is load-bearing for any host function that calls `ctx.eval()` — removing or refactoring it breaks `test_reentrant_eval_from_host_function` and `test_host_function_args_are_copied_before_dispatch`. See step 5 in §15.

### 6.8 What rquickjs gives us for free

Things the 1800-line C shim did by hand that rquickjs handles:

- `JSValue` refcounting → Rust ownership/Drop
- `JSContext` / `JSRuntime` lifecycle → rquickjs `Runtime` / `Context` RAII
- Type inspection (`typeof`) → `Value::type_of()`
- Property access → `Object::get()` / `Object::set()`
- Function calls → `Function::call()`
- Constructor calls → `Function::construct()`
- Promise creation → `Promise::wrap_future()` or manual `PromiseCapability`
- Promise state inspection → `Promise::state()`
- Pending job execution → `Context::execute_pending_job()`
- Memory limit → `Runtime::set_memory_limit()`
- Stack limit → `Runtime::set_max_stack_size()`
- Interrupt handler → `Runtime::set_interrupt_handler()`
- BigInt → `BigInt` type with `to_string()` / `from_str()`
- TypedArray → `TypedArray<u8>` with `.as_bytes()`

What's left for us to write in Rust: the PyO3 boilerplate (`#[pyclass]`, `#[pymethods]`), the recursive marshaling between PyO3 types and rquickjs types (§6.6), the host-function trampoline (§6.5), and GIL management for the interrupt handler and host callbacks.

**Estimated Rust: 400-600 lines in a single `src/lib.rs`.** Compared to 1800 lines of C. And Rust with rquickjs handling lifecycles, not raw C with manual refcounting.

## 7. Python API

**Unchanged from v0.2 §7.2.** Every class, method, property, and behavioral contract carries over. The changes are internal:

- `quickjs_rs.runtime.Runtime.__init__` creates a `_engine.QjsRuntime` instead of loading a wasm module.
- `quickjs_rs.context.Context.__init__` creates a `_engine.QjsContext` instead of instantiating a wasm context through the bridge.
- `quickjs_rs.handle.Handle` wraps a `_engine.QjsHandle` instead of a slot ID.
- `quickjs_rs.context.Context._dispatch_host_call` is the fn_id→callable dispatcher, structurally identical to the wasm version.
- `quickjs_rs.context.Context.eval_async` driving loop is unchanged — same asyncio.TaskGroup, same Event signaling, same deadlock/concurrent-eval detection.
- `quickjs_rs.errors` is unchanged — pure Python classes.

## 8. Limits and safety

| Limit | Default | Enforcement |
|---|---|---|
| Memory | 64 MB | rquickjs `Runtime::set_memory_limit` → QuickJS `JS_SetMemoryLimit` |
| Stack | 1 MB | rquickjs `Runtime::set_max_stack_size` → QuickJS `JS_SetMaxStackSize` |
| Timeout | 5 s | Python-side interrupt handler via `set_interrupt_handler`, same wall-clock check |
| Max handles | Unbounded (rquickjs refcounting is automatic) | No slot table cap needed; rquickjs uses Rust ownership |

**No WASI stubs.** There's no WASI layer. QuickJS has no built-in system APIs, so there's nothing to deny. The "no fs, no net" property comes from QuickJS itself, not from WASI stub configuration. Host functions are the only way JS code can interact with the outside world, and you control which ones are registered.

**No wasm epoch interruption.** The backup timeout mechanism from the wasm architecture (wasmtime epoch, 50 ms cadence) is gone. The interrupt handler is the sole timeout enforcement mechanism. This is simpler and fine — the interrupt handler is polled by QuickJS's bytecode dispatch loop, which is the same mechanism that runs in the wasm version. The wasmtime epoch was only there as a backup for the case where QuickJS's interrupt hook "somehow didn't fire" — a defensive measure that never triggered in practice.

**Thread safety.** rquickjs's `Runtime` is `Send` but not `Sync` — it can move between threads but can't be shared. `Context` is neither `Send` nor `Sync`. This matches our existing contract: one `eval_async` per context, no concurrent access. The Python-side `ConcurrentEvalError` guard is the enforcement layer.

## 9. Errors

Unchanged from v0.2 §10. The error hierarchy, the exception classification logic (InternalError → TimeoutError/MemoryLimitError, HostError with `__cause__`), the HostCancellationError semantics — all transfer. The only difference is that the bridge no longer needs to parse msgpack-encoded exception records — rquickjs surfaces QuickJS exceptions as Rust `Error` types that PyO3 translates to Python exceptions.

## 10. Testing

The entire test suite from `quickjs-wasm` transfers. Changes:

- `import quickjs_wasm` → `import quickjs_rs` (mechanical find-replace)
- `conftest.py` fixtures are unchanged (they use the public API, not bridge internals)
- No more `_bridge` or `_msgpack` internal tests (those modules don't exist)
- Bridge-level tests from v0.2 step 3 (manual pump, host_call_async dispatch) are replaced by equivalent tests against `_engine` directly, if needed

The acceptance tests (§13.1, §13.2) are the primary verification that the rewrite is correct. If they pass, the rewrite preserves behavior.

### CI matrix

Python 3.11, 3.12, 3.13 × Linux (manylinux), macOS (x86_64 + arm64), Windows. Maturin-action builds platform-specific wheels. Tests run per-platform.

## 11. Benchmarks

The benchmark suite transfers. Expected improvements based on architecture change:

| Benchmark | wasm (v0.2) | native (target) | Why |
|---|---|---|---|
| `bench_runtime_create` | ~1 s | < 5 ms | No wasm JIT; just C library init |
| `bench_context_create` | ~74 µs | < 20 µs | No wasm boundary |
| `bench_eval_noop` | ~110 µs | < 15 µs | Single FFI crossing vs four |
| `bench_eval_arithmetic` | ~110 µs | < 15 µs | Same |
| `bench_host_call_noop` | ~134 µs | < 25 µs | No msgpack encode/decode |
| `bench_host_call_100x_loop` | ~7.5 ms | < 2 ms | Same |
| `bench_eval_async_noop` | ~1 ms | < 200 µs | Async overhead stays, bridge overhead drops |
| `bench_marshal_dict_flat_100` | ~492 µs | < 100 µs | Direct PyO3↔rquickjs, no msgpack |

The first benchmark run after the rewrite is the most important one — it validates the architectural thesis. If `bench_runtime_create` doesn't drop to single-digit milliseconds, something is wrong with the build configuration (debug symbols, missing LTO, etc.).

## 12. Dependencies

```toml
[project]
name = "quickjs-rs"
version = "0.3.0"
requires-python = ">=3.11"
# Zero runtime dependencies

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov",
    "pytest-asyncio>=0.23",
    "ruff",
    "mypy",
]
bench = [
    "pytest-codspeed>=3.0",
]
```

**Zero runtime dependencies.** Compare to quickjs-wasm's two (`wasmtime>=27`, `msgpack>=1.1`). The native extension is self-contained.

## 13. Acceptance criteria

§13.1 (v0.1 sync acceptance) and §13.2 (v0.2 async acceptance) from the v0.2 spec, with `quickjs_wasm` → `quickjs_rs` import changes. Both must pass.

Additionally:

- `bench_runtime_create` < 10 ms (validates the cold-start thesis)
- `bench_eval_noop` < 30 µs (validates the eval-floor thesis)
- `bench_host_call_noop` < 50 µs (validates the marshaling thesis)
- Zero runtime dependencies in `pip show quickjs-rs`
- `maturin build --release` produces a working wheel on Linux, macOS (both arches), and Windows
- `mypy quickjs_rs` clean, `ruff check` clean

## 14. Migration guide (quickjs-wasm → quickjs-rs)

```bash
pip uninstall quickjs-wasm
pip install quickjs-rs
```

```python
# Before
from quickjs_wasm import Runtime, Context, Handle, JSError
# After
from quickjs_rs import Runtime, Context, Handle, JSError
```

No API changes. No behavioral changes. Faster everything.

For users who need the wasm sandbox (processing adversarial JS from untrusted internet sources): stay on `quickjs-wasm` v0.2 or wait for a future version that reintroduces the wasm layer on top of the Rust codebase. For agent workloads where JS is generated by models you control: migrate.

## 15. Implementation order

### Phase 0: transition scaffolding (same repo)

0a. Replace `spec/implementation.md` with this document. Commit as `spec: v0.3 rewrite spec — PyO3 + rquickjs, drop wasm layer`.
0b. Rename `quickjs_wasm/` → `quickjs_rs/`. Update all imports in `tests/`, `benchmarks/`, and inside the package itself. Verify `pytest` still collects (tests will fail — the bridge is gone, but they should collect and error cleanly). Commit as `refactor: rename package quickjs_wasm → quickjs_rs`.
0c. Remove the wasm layer: delete `wasm/`, `vendor/`, `quickjs_rs/_bridge.py`, `quickjs_rs/_msgpack.py`, `quickjs_rs/_resources/`, `scripts/install-wasi-sdk.sh`, `scripts/verify-reproducible.sh`, `scripts/update-quickjs.sh`, `.github/workflows/build-wasm.yml`. Remove `wasmtime` and `msgpack` from `pyproject.toml` dependencies. Remove the git submodule entry for `vendor/quickjs-ng` (`git rm vendor/quickjs-ng`). Commit as `refactor: remove wasm layer, C shim, and related tooling`.
0d. Add `Cargo.toml` per §5.3. Update `pyproject.toml` to use maturin build backend per §5.4. Verify `maturin develop` compiles rquickjs and produces a loadable `_engine` module (even if it exposes nothing yet). Commit as `build: maturin + rquickjs setup`.
0e. Update CLAUDE.md for v0.3 rewrite context. Commit as `docs: update CLAUDE.md for v0.3 rewrite`.

At the end of phase 0: the repo has the new build system, the old wasm code is gone, the package is renamed, tests fail cleanly (ImportError on `_engine` or missing bridge), and the spec documents the plan. Five small commits, each internally consistent.

### Phase 1: Rust extension + sync API (turns §13.1 green)

1. Implement `QjsRuntime` — new, set_memory_limit, set_stack_limit, set_interrupt_handler, close. Smoke test: create a runtime, set limits, close. Commit.
2. Implement `QjsContext` — new, eval (sync, primitives only), close. Wire `quickjs_rs/runtime.py` and `quickjs_rs/context.py` to use `_engine.QjsRuntime` and `_engine.QjsContext`. First acceptance assertion: `ctx.eval("1 + 2") == 3`. Commit.
3. Widen eval marshaling: strings, booleans, null, undefined, bigint, bytes, arrays, objects. Same assertion-by-assertion progression as the original v0.1 implementation. Commit per meaningful group.
4. Implement globals — global_object handle, get/set via QjsHandle, Globals proxy in Python. Commit.
5. Implement host functions — register_host_function, fn_id dispatch, sync path. `@ctx.function` decorator in Python. Commit.
6. Implement error propagation — JS exceptions to Python (JSError, MemoryLimitError, TimeoutError, InterruptError), Python exceptions to JS (HostError with __cause__). Commit.
7. Implement handles — QjsHandle with get_prop, set_prop, call, call_method, new_instance, to_python, type_of, dispose, dup. Handle lifecycle (ResourceWarning, cross-context guard). Commit.
8. Run §13.1 acceptance test. Fix anything that fails. Commit when fully green.

### Phase 2: async API (turns §13.2 green)

9. Implement async host functions — register_host_function with is_async, resolve_pending, reject_pending, promise_state, promise_result in Rust. eval_async driving loop in Python (carried forward from v0.2, adapted for _engine API). Commit.
10. Run §13.2 acceptance test. Fix anything that fails. Commit when fully green.

### Phase 3: verification and release

11. Verify all test files pass (test_primitives through test_async_host_functions). Fix any failures. Commit.
12. Run benchmarks. Record baseline numbers. Compare against v0.2 wasm numbers — validate the architectural thesis (§11 targets). Commit with numbers in body.
13. Update CI workflows for maturin (test matrix with maturin-action, benchmark workflow, release workflow with multi-platform wheel builds). Commit.
14. Tag v0.3.0-rc1. Soak. Tag v0.3.0 after soak passes.

## 16. Open decisions

- **Publish both package names?** `quickjs-wasm` stays on PyPI at v0.2 for users who want the sandbox. `quickjs-rs` launches at v0.3 as the recommended default. Maintenance burden: two packages, but `quickjs-wasm` is frozen (no new features, security-fix-only). Lean: yes, publish both. The old package could also be turned into a thin redirect that depends on `quickjs-rs` with a deprecation warning on import.
- **QuickJS version pinning.** rquickjs vendors quickjs-ng internally. We don't control the pin directly — rquickjs releases include a specific quickjs-ng version. Acceptable if rquickjs updates promptly on security fixes. Verify rquickjs's update cadence before committing; if it's slow, consider forking rquickjs or using `rquickjs-sys` directly with our own pin.
- **GIL release during eval.** Releasing the GIL during `eval` allows other Python threads to run while JS executes. This is correct for CPU-bound JS, but the interrupt handler callback needs the GIL back. rquickjs's interrupt handler runs on the same thread as eval, so acquiring the GIL from within the interrupt callback should work (same thread, reentrant acquisition). Verify this doesn't deadlock under Python's GIL semantics.
- **free-threaded Python (3.13t).** PyO3 0.22 has experimental free-threaded support. rquickjs's `Runtime` is not `Sync`, which aligns with our single-eval-per-context rule. But free-threaded Python changes the GIL assumptions in §6.7. Defer to v0.4; test and fix if it breaks.