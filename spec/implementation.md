# quickjs-wasm implementation spec

Version: 0.1.0 target
Status: ready to build
Companion docs: `quickjs.wit` (interface contract), `quickjs-py-spec.md` (design rationale)

## 1. Project overview

`quickjs-wasm` is a Python library for safely executing untrusted JavaScript from a Python host, modeled on `quickjs-emscripten` but hosted directly from Python via a WASI build of QuickJS. Primary consumer: interpreter middleware for Python agent frameworks (langgraph, deepagents). Distribution: PyPI package, pure-Python wheel, bundles a single architecture-independent `quickjs.wasm`.

**Name**: `quickjs-wasm` (PyPI) / `quickjs_wasm` (import).

**License**: MIT, matching QuickJS upstream.

**Python version**: 3.10+. Uses `match` statements and modern type hint syntax.

**v0.1 scope**: synchronous eval, globals, handles, primitives + structured values, sync host functions, memory/timeout limits, JS↔Python error propagation. See §13 for acceptance criteria.

**Explicit non-goals for v0.1**: async/await, ES module loading from disk, host classes with prototypes, debugger, SharedArrayBuffer, component-model packaging. See §14 for roadmap.

## 2. Architecture

Three layers, bottom up:

1. **`quickjs.wasm`** — QuickJS compiled with WASI-SDK plus a C shim (`shim.c`) that exposes QuickJS's API as wasm exports and declares wasm imports for host callbacks. Roughly 1 MB gzipped. Architecture-independent; ships verbatim in the Python wheel.

2. **`quickjs_wasm._bridge`** — Python module that loads `quickjs.wasm` via `wasmtime-py`, wires WASI (denied by default: no FS, no net, no clock, no stdio, no env), manages the host function registry, dispatches host calls. Direct wrapper over wasmtime bindings; no user-facing API.

3. **`quickjs_wasm`** (top-level) — user-facing Python API. `Runtime`, `Context`, `Handle`, error types, value marshaling.

The WIT file `quickjs.wit` is the authoritative interface definition between (1) and (2). v0.1 implements the shape by hand using raw WASI because `wasmtime-py`'s component-model bindings are still maturing; the WIT exists as spec documentation and as the migration target for v0.3+.

## 3. Repository layout

```
quickjs-wasm/
├── README.md
├── LICENSE
├── pyproject.toml
├── spec/
│   ├── quickjs.wit                 # Interface contract (authoritative)
│   ├── implementation.md           # This document
│   └── design.md                   # Original design doc
├── vendor/
│   └── quickjs-ng/                 # Pinned git submodule
├── wasm/
│   ├── CMakeLists.txt
│   ├── wasi-sdk.cmake              # Toolchain file
│   ├── shim.c                      # The C shim
│   ├── shim.h
│   └── build.sh                    # Wrapper: cmake → wasm-opt → strip
├── quickjs_wasm/
│   ├── __init__.py                 # Public re-exports
│   ├── _bridge.py                  # wasmtime wiring, import/export glue
│   ├── _msgpack.py                 # Marshaling: Python ↔ MessagePack
│   ├── _resources/
│   │   └── quickjs.wasm            # Built artifact, shipped in wheel
│   ├── runtime.py                  # Runtime
│   ├── context.py                  # Context
│   ├── handle.py                   # Handle
│   ├── globals.py                  # Globals mapping proxy
│   ├── errors.py                   # Exception hierarchy
│   └── py.typed
├── tests/
│   ├── conftest.py
│   ├── test_primitives.py
│   ├── test_objects.py
│   ├── test_host_functions.py
│   ├── test_exceptions.py
│   ├── test_limits.py
│   ├── test_handles.py
│   └── test_globals.py
├── scripts/
│   ├── update-quickjs.sh           # Bump the submodule
│   └── verify-reproducible.sh      # Byte-compare a rebuild
└── .github/
    └── workflows/
        ├── build-wasm.yml          # Builds quickjs.wasm, attaches to release
        ├── test.yml                # pytest on matrix
        └── release.yml             # Wheel to PyPI
```

## 4. Toolchain and build

### 4.1 Pinned versions

| Dependency | Version | Notes |
|---|---|---|
| QuickJS source | `quickjs-ng/quickjs` @ a pinned commit | ng fork — better WASI support and active maintenance |
| WASI-SDK | 24 | Clang toolchain for wasm32-wasi |
| CMake | 3.24+ | |
| wasm-opt (binaryen) | 119+ | |
| Python | 3.10+ | |
| wasmtime-py | 27+ | Component support not required for v0.1 |
| msgpack | 1.1+ | Pure Python fine; C ext preferred |
| pytest | 8+ | |

### 4.2 Build commands

```bash
# One-time setup
git submodule update --init --recursive
./scripts/install-wasi-sdk.sh       # downloads and unpacks to ./toolchain

# Build the wasm
cd wasm
./build.sh                          # → ../quickjs_wasm/_resources/quickjs.wasm

# Build the wheel
cd ..
pip install build
python -m build                     # → dist/quickjs_wasm-0.1.0-py3-none-any.whl

# Run tests
pip install -e ".[dev]"
pytest
```

### 4.3 Reproducibility

The wasm build must be byte-reproducible given the same pinned inputs. `SOURCE_DATE_EPOCH=0`, `-Wl,--no-deterministic-unsigned-leb` where applicable, strip after `wasm-opt -O3`. CI runs `verify-reproducible.sh` which rebuilds and byte-diffs against the committed `quickjs.wasm.sha256`.

## 5. WIT contract

Full WIT in `spec/quickjs.wit`. Summary of semantics the implementation must preserve:

- **Runtime** owns the JS heap and memory/stack limits. One heap per runtime.
- **Context** is cheap, shares the runtime's heap. Handles are scoped to their creating context.
- **Handle** is a typed opaque reference to an in-guest `JSValue` with explicit lifetime. Dropping a handle releases its slot.
- **Value** is the marshaled form: null, undefined, boolean, number (f64), bigint (decimal string), string, bytes, list, object (ordered key/value pairs).
- **js-error** carries name, message, optional stack.
- **host-call** is the single dispatch point for registered host functions; takes a fn-id and argument list, returns value or error.
- **host-interrupt** is polled during JS execution; returning true aborts.

## 6. C shim specification

The shim lives in `wasm/shim.c`, compiles to `quickjs.wasm`, links `libquickjs.a` from the vendored source. All public functions are prefixed `qjs_`. Types are wasm32-appropriate: `uint32_t` for pointers and handles, `uint64_t` for larger quantities.

### 6.1 Slot table

JSValues don't cross the wasm boundary as structs. The shim maintains a per-runtime slot table mapping `uint32_t slot_id → JSValue`. All shim functions that produce or consume JS values use slot IDs. Slot 0 is reserved (invalid/null). Slots are reference-counted; `qjs_slot_dup` increments, `qjs_slot_drop` decrements and frees at zero. The shim owns the `JSValue`'s refcount contribution.

### 6.2 Exports

Return type convention: `int32_t` status where `0 = ok`, `1 = JS exception raised`, negative = shim error (OOM, invalid slot, etc.). Out-params use pointers into guest memory.

```c
/* Runtime lifecycle */
uint32_t qjs_runtime_new(void);
void     qjs_runtime_free(uint32_t rt);
void     qjs_runtime_set_memory_limit(uint32_t rt, uint64_t bytes);
void     qjs_runtime_set_stack_limit(uint32_t rt, uint64_t bytes);
int32_t  qjs_runtime_run_pending_jobs(uint32_t rt, uint32_t *out_count);
bool     qjs_runtime_has_pending_jobs(uint32_t rt);
void     qjs_runtime_install_interrupt(uint32_t rt);

/* Context lifecycle */
uint32_t qjs_context_new(uint32_t rt);
void     qjs_context_free(uint32_t ctx);

/* Slot management */
uint32_t qjs_slot_dup(uint32_t ctx, uint32_t slot);
void     qjs_slot_drop(uint32_t ctx, uint32_t slot);

/* Eval.
 * flags: bit 0 = module, bit 1 = compile-only, bit 2 = strict.
 * On success (0), *out_slot is the result.
 * On exception (1), *out_slot is the exception (call qjs_exception_to_msgpack to extract).
 */
int32_t qjs_eval(uint32_t ctx, uint32_t code_ptr, uint32_t code_len,
                 uint32_t flags, uint32_t *out_slot);

/* Globals and property access */
int32_t qjs_get_global_object(uint32_t ctx, uint32_t *out_slot);
int32_t qjs_get_prop(uint32_t ctx, uint32_t obj_slot,
                     uint32_t key_ptr, uint32_t key_len, uint32_t *out_slot);
int32_t qjs_set_prop(uint32_t ctx, uint32_t obj_slot,
                     uint32_t key_ptr, uint32_t key_len, uint32_t val_slot);
int32_t qjs_get_prop_u32(uint32_t ctx, uint32_t obj_slot,
                         uint32_t index, uint32_t *out_slot);

/* Function invocation */
int32_t qjs_call(uint32_t ctx, uint32_t fn_slot, uint32_t this_slot,
                 uint32_t argc, uint32_t argv_ptr, uint32_t *out_slot);
int32_t qjs_new_instance(uint32_t ctx, uint32_t ctor_slot,
                         uint32_t argc, uint32_t argv_ptr, uint32_t *out_slot);

/* Marshaling.
 * qjs_to_msgpack writes MessagePack bytes into a per-context scratch buffer
 * owned by the shim. *out_ptr, *out_len are valid until the next marshaling
 * call on this context.
 */
int32_t qjs_to_msgpack(uint32_t ctx, uint32_t slot,
                       uint32_t *out_ptr, uint32_t *out_len);
int32_t qjs_from_msgpack(uint32_t ctx, uint32_t data_ptr, uint32_t data_len,
                         uint32_t *out_slot);
int32_t qjs_exception_to_msgpack(uint32_t ctx, uint32_t exc_slot,
                                 uint32_t *out_ptr, uint32_t *out_len);
```

The scratch buffer is per-context, grows in place via `realloc` when a value exceeds current capacity, and starts at 64 KB. There is no separate malloc-and-return path for large values — the single ownership model (scratch owned by shim, invalidated on next marshaling call on this context) applies uniformly. Callers never free marshaling output.

```c

/* Type inspection (does not traverse). Returns a qjs_value_kind. */
uint32_t qjs_type_of(uint32_t ctx, uint32_t slot);
bool     qjs_is_promise(uint32_t ctx, uint32_t slot);
/* 0 = pending, 1 = fulfilled, 2 = rejected, -1 = not a promise */
int32_t  qjs_promise_state(uint32_t ctx, uint32_t slot);

/* Host function registration.
 * Creates a JS global `name` that, when called, dispatches to `host_call`
 * with fn_id and marshaled args.
 */
int32_t qjs_register_host_function(uint32_t ctx,
                                   uint32_t name_ptr, uint32_t name_len,
                                   uint32_t fn_id);

/* Guest memory allocation for host-provided buffers.
 * qjs_malloc returns a guest pointer the host can write into, then passes
 * that pointer back into eval/set_prop/etc. The host must qjs_free it
 * (unless ownership is explicitly transferred, which is documented per
 * function — none transfer in v0.1).
 */
uint32_t qjs_malloc(uint32_t size);
void     qjs_free(uint32_t ptr);
```

### 6.3 Imports (declared by the shim, provided by the Python host)

```c
/* Dispatched when JS calls a host-registered function.
 * args: MessagePack-encoded list of argument values.
 * Returns 0 on host success, 1 if the host raised, negative on marshaling failure.
 * On non-error return, *out_ptr / *out_len point into a host-provided buffer
 * (inside guest memory, allocated via qjs_malloc by the host before return)
 * containing the MessagePack-encoded return value or error record.
 * The shim calls qjs_free on that buffer after reading it.
 */
__attribute__((import_name("host_call")))
int32_t host_call(uint32_t fn_id,
                  uint32_t args_ptr, uint32_t args_len,
                  uint32_t *out_ptr, uint32_t *out_len);

/* Called periodically by the QuickJS interrupt handler. Non-zero return
 * causes QuickJS to abort execution with an InterruptError.
 */
__attribute__((import_name("host_interrupt")))
int32_t host_interrupt(void);
```

### 6.4 Invariants the shim enforces

- All exported functions that accept a slot ID validate that the slot is live; return negative status on invalid slot (never crash).
- The scratch MessagePack buffer for `qjs_to_msgpack` belongs to the context; subsequent calls invalidate it. Python side must fully read before making another call.
- `qjs_runtime_install_interrupt` must be called before any `qjs_eval` that should be interruptible; in practice the Python `Runtime` calls it in its constructor path.
- QuickJS is built with `CONFIG_BIGNUM=y`. BigInts are supported.
- `qjs_eval` internally NUL-pads its input before handing to the underlying QuickJS parser; callers pass raw `(code_ptr, code_len)` without trailing-NUL padding. QuickJS's tokenizer does one-past-end lookahead during token scanning despite taking an explicit length, and without the pad it trips a spurious `SyntaxError: invalid UTF-8 sequence` on whatever uninitialized byte follows. The shim absorbs this so no caller — Python bridge, future WIT-component host, test harness — has to remember.

## 7. Python package

### 7.1 Module layout

```
quickjs_wasm/
  __init__.py        # Re-exports; version
  _bridge.py         # wasmtime wiring (internal)
  _msgpack.py        # Marshaling (internal)
  runtime.py         # class Runtime
  context.py         # class Context
  handle.py          # class Handle
  globals.py         # class Globals (dict-like proxy)
  errors.py          # Exception hierarchy
```

### 7.2 Public API

```python
# quickjs_wasm/__init__.py
from quickjs_wasm.runtime import Runtime
from quickjs_wasm.context import Context
from quickjs_wasm.handle import Handle
from quickjs_wasm.errors import (
    QuickJSError, JSError, HostError, MarshalError,
    InterruptError, MemoryLimitError, TimeoutError, InvalidHandleError,
)

__version__ = "0.1.0"
__all__ = [
    "Runtime", "Context", "Handle",
    "QuickJSError", "JSError", "HostError", "MarshalError",
    "InterruptError", "MemoryLimitError", "TimeoutError", "InvalidHandleError",
]
```

```python
# quickjs_wasm/runtime.py
class Runtime:
    def __init__(
        self,
        *,
        memory_limit: int | None = 64 * 1024 * 1024,   # 64 MB default
        stack_limit: int | None = 1 * 1024 * 1024,     # 1 MB default
    ) -> None: ...

    def __enter__(self) -> "Runtime": ...
    def __exit__(self, *exc) -> None: ...
    def close(self) -> None: ...

    def new_context(self, *, timeout: float = 5.0) -> "Context": ...

    def run_pending_jobs(self) -> int: ...
    @property
    def has_pending_jobs(self) -> bool: ...
```

```python
# quickjs_wasm/context.py
from typing import Any, Callable, overload

class Context:
    # Users should prefer Runtime.new_context(); direct construction is supported for advanced use.
    def __init__(self, runtime: Runtime, *, timeout: float = 5.0) -> None: ...

    def __enter__(self) -> "Context": ...
    def __exit__(self, *exc) -> None: ...
    def close(self) -> None: ...

    def eval(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> Any:
        """Evaluate and return a marshaled Python value.

        Raises JSError on JS exception, MarshalError if the result contains
        a function/symbol/circular reference (use eval_handle instead),
        TimeoutError if the configured timeout elapses,
        MemoryLimitError if the runtime memory limit is hit.
        """

    def eval_handle(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> "Handle": ...

    @property
    def globals(self) -> "Globals": ...

    @overload
    def function(self, fn: Callable[..., Any]) -> Callable[..., Any]: ...
    @overload
    def function(self, *, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        """Register a Python callable as a JS global function named `name`."""

    @property
    def timeout(self) -> float: ...
    @timeout.setter
    def timeout(self, value: float) -> None: ...
```

```python
# quickjs_wasm/handle.py
from typing import Any, Literal

ValueKind = Literal[
    "null", "undefined", "boolean", "number", "bigint",
    "string", "symbol", "object", "function", "array",
]

class Handle:
    def __enter__(self) -> "Handle": ...
    def __exit__(self, *exc) -> None: ...
    def __del__(self) -> None: ...  # emits ResourceWarning if not disposed
    def dispose(self) -> None: ...

    @property
    def disposed(self) -> bool: ...
    @property
    def type_of(self) -> ValueKind: ...
    @property
    def is_promise(self) -> bool: ...

    def get(self, key: str | int) -> "Handle": ...
    def set(self, key: str, value: "Handle | Any") -> None: ...
    def call(self, *args: "Handle | Any", this: "Handle | None" = None) -> "Handle": ...
    def call_method(self, name: str, *args: "Handle | Any") -> "Handle": ...
    def new(self, *args: "Handle | Any") -> "Handle": ...

    def to_python(self, *, allow_opaque: bool = False) -> Any:
        """Marshal this handle out to a Python value.

        Raises MarshalError if the handle holds a function, symbol, or
        circular reference, unless allow_opaque=True, which replaces those
        with child Handle instances in the returned structure.
        """

    def await_promise(self, *, deadline: float | None = None) -> "Handle":
        """Drive pending jobs until this promise settles.

        v0.1: raises NotImplementedError. Stubbed for API stability; lands in v0.3.
        """
```

```python
# quickjs_wasm/globals.py
class Globals:
    """Dict-like proxy for the JS global object.

    Reads return marshaled Python values; writes accept Python values or Handles.
    """
    def __getitem__(self, key: str) -> Any: ...
    def __setitem__(self, key: str, value: "Handle | Any") -> None: ...
    def __contains__(self, key: str) -> bool: ...
    def get_handle(self, key: str) -> "Handle": ...
```

```python
# quickjs_wasm/errors.py
class QuickJSError(Exception):
    """Base class for all errors raised by quickjs-wasm."""

class JSError(QuickJSError):
    """A JS exception propagated to Python.

    Attributes:
        name: JS error name (TypeError, RangeError, etc.)
        message: JS error message
        stack: JS stack trace string, or None
    """
    name: str
    message: str
    stack: str | None

class HostError(JSError):
    """A Python exception raised inside a registered host function that
    escaped back out to Python. `__cause__` is the original Python exception.
    """

class MarshalError(QuickJSError):
    """A value could not be marshaled (function in eval result, circular ref)."""

class InterruptError(QuickJSError):
    """JS execution was interrupted by the host."""

class TimeoutError(InterruptError):
    """The context's timeout elapsed during execution."""

class MemoryLimitError(QuickJSError):
    """The runtime's memory limit was exceeded."""

class InvalidHandleError(QuickJSError):
    """A Handle was used after dispose() or across contexts."""
```

### 7.3 Behavioral specifics

**Context manager semantics.** `Runtime.__exit__` closes all contexts created by it. `Context.__exit__` disposes all outstanding handles and closes the context. Calling `close()` twice is a no-op.

**Handle ownership.** Handles are tied to the context that created them. Using a handle from context A in a call on context B raises `InvalidHandleError`. When a handle is garbage-collected without explicit dispose, `__del__` calls `dispose()` and emits a `ResourceWarning` — this is Python convention for leaked resources (see `warnings.filterwarnings("error", category=ResourceWarning)` in tests).

**Global mapping proxy.** `ctx.globals["x"] = 1` performs a single `set-global`. Reads perform `get-global` each time (no caching). `ctx.globals.get_handle("x")` returns a handle instead of a marshaled value.

**Function registration.** `@ctx.function` uses `fn.__name__` unless overridden. Keyword-only `name=` when used as `@ctx.function(name="myFn")`. Registered functions see Python-native args (already marshaled from MessagePack) and return Python-native values (marshaled back). Type coercion follows §8.

**Timeout.** Installed per-context. Timeout is measured in wall-clock seconds from the start of each `eval` / `eval_handle` / `Handle.call` call. Cleared when the outer call returns. `host_interrupt` implementation: compare `time.monotonic()` to the stored deadline, return 1 if exceeded.

**Memory limit.** Per-runtime. Exceeding it causes the current JS operation to raise `MemoryLimitError` in Python (QuickJS returns an out-of-memory exception, which we intercept and rewrap).

## 8. MessagePack encoding

Values cross the shim boundary as MessagePack. We use standard types plus three ext codes for JS-specific semantics:

| JS type | MessagePack representation |
|---|---|
| `null` | `nil` |
| `undefined` | ext type 0, empty body |
| `boolean` | `bool` |
| `number` | `float64` (always f64, even for integer values, to preserve JS semantics) |
| `bigint` | ext type 1, body is UTF-8 decimal string |
| `string` | `str` |
| `Uint8Array` | `bin` |
| `Array` | `array` |
| plain `Object` | `map` with `str` keys (insertion-ordered) |

Sparse arrays are encoded densely; holes become `undefined`. This matches `JSON.stringify` and is the natural mapping given msgpack has no notion of a hole. Callers that need to distinguish holes from explicit `undefined` should use `eval_handle` and traverse the object directly.

Python side:

| Python type | Marshal to JS as |
|---|---|
| `None` | `null` |
| `bool` | `boolean` |
| `int` in [-2^53+1, 2^53-1] | `number` |
| `int` outside that range | `bigint` |
| `float` | `number` |
| `str` | `string` |
| `bytes` / `bytearray` / `memoryview` | `Uint8Array` |
| `list` / `tuple` | `Array` |
| `dict` (str keys) | `Object` |
| `quickjs_wasm.Undefined` singleton | `undefined` |

Unmarshalable Python types (sets, custom classes, datetimes, etc.) raise `MarshalError` at the point of marshaling. v0.1 does not support a `default` hook; add in v0.2 if needed.

`Context.eval(...)` defaults to converting `undefined` → `None`. Set `ctx.preserve_undefined = True` to get the `Undefined` singleton instead.

## 9. Limits and safety defaults

| Limit | Default | Scope | Enforcement |
|---|---|---|---|
| Memory | 64 MB | Runtime | QuickJS `JS_SetMemoryLimit` |
| Stack | 1 MB | Runtime | QuickJS `JS_SetMaxStackSize` |
| Timeout | 5 s | Context | `host_interrupt` wall-clock check |
| Wasm epoch | bound to timeout | Runtime | Wasmtime epoch deadline (backup) |
| Max slots | 1M per context | Context | Shim-side slot table cap, raises on overflow |

Defaults are chosen for agent interpreter workloads. All are overridable.

**WASI stubs.** The bridge provides the following WASI imports, all denied:

- `fd_*`: all return `EBADF` except `fd_write` to stdout/stderr which drops output.
- `path_*`: return `ENOENT`.
- `poll_oneoff`: returns `ENOSYS`.
- `clock_time_get`: returns a fixed epoch (0) unless `Runtime(allow_clock=True)`.
- `random_get`: returns from `os.urandom`. JS `Math.random` works.
- `environ_get` / `args_get`: return empty.
- `proc_exit`: raises `InterruptError` host-side.

This means the guest has no filesystem, no network, no real clock by default, and cannot exit the process.

## 10. Errors

### 10.1 From JS to Python

When QuickJS returns an exception status (`qjs_eval` returns 1), the bridge calls `qjs_exception_to_msgpack` to get `{name, message, stack}` and raises:

- `InterruptError` / `TimeoutError` if `name == "InternalError"` and message contains the interrupt marker
- `MemoryLimitError` if `name == "InternalError"` and message contains the OOM marker (QuickJS emits a specific string for `JS_ATOM_out_of_memory`)
- `JSError` otherwise, populated with `name`, `message`, `stack`

### 10.2 From Python host functions to JS

When a registered host function raises:

1. The bridge catches the Python exception
2. Encodes it as a JS error record: `{name: "HostError", message: str(exc), stack: traceback}`
3. Returns it via `host_call` with status=1
4. The shim's host-function wrapper throws it in JS

If that JS error propagates back out to Python uncaught, it's rewrapped as `HostError` with `__cause__` set to the original Python exception.

### 10.3 Invariant

Every public Python method either returns successfully or raises a `QuickJSError` subclass. No bare `Exception`, no `RuntimeError` leaking from the bridge.

## 11. Testing strategy

Pytest. `tests/conftest.py` provides:

```python
@pytest.fixture
def rt():
    with Runtime() as rt:
        yield rt

@pytest.fixture
def ctx(rt):
    with rt.new_context() as ctx:
        yield ctx

@pytest.fixture(autouse=True)
def strict_warnings():
    import warnings
    warnings.filterwarnings("error", category=ResourceWarning)
```

### 11.1 Test files and what each covers

**`test_primitives.py`** — number, string, bool, null, undefined, bigint, bytes round-trip. JS → Python and Python → JS via globals.

**`test_objects.py`** — plain objects, arrays, nested structures, key insertion order preservation, Uint8Array round-trip.

**`test_host_functions.py`** — register sync function, JS calls it, return values propagate, exceptions propagate. Decorator and explicit `register`. `name=` override. Positional and keyword args (JS has no kwargs — only positional, document this).

**`test_exceptions.py`** — `throw new TypeError()` raises `JSError` with correct `name`/`message`/`stack`. Python exception in host fn raises `HostError` with `__cause__`. JS catching a `HostError` with try/catch and inspecting it.

**`test_limits.py`** — memory limit triggers `MemoryLimitError`. Timeout triggers `TimeoutError`. Stack overflow in JS raises `JSError(name="InternalError")` (not the memory limit).

**`test_handles.py`** — handle lifecycle: dispose, context manager, `__del__` emits `ResourceWarning`, cross-context use raises `InvalidHandleError`, `to_python` traverses correctly, `allow_opaque=True` keeps functions as handles.

**`test_globals.py`** — get/set/contains, `get_handle`, handle-backed assignments.

### 11.2 Integration smoke test

`tests/test_smoke.py` runs the acceptance criteria from §13 end-to-end as a single test, providing a tripwire if anything fundamental regresses.

### 11.3 CI matrix

GitHub Actions. Matrix: Python 3.10, 3.11, 3.12, 3.13 × Linux, macOS, Windows. Single wasm artifact built once on Linux and cached; tests download and use it. Wheel build/publish on tag.

## 12. Dependencies

`pyproject.toml`:

```toml
[project]
name = "quickjs-wasm"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "wasmtime>=27.0.0",
    "msgpack>=1.1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov",
    "ruff",
    "mypy",
]
```

No other runtime deps. Wheel is `py3-none-any` — platform-independent because wasmtime-py ships native extensions and `quickjs.wasm` is architecture-independent.

## 13. v0.1 acceptance criteria

The following script must run to completion without unexpected warnings or errors:

```python
import pytest
from quickjs_wasm import (
    Runtime, JSError, HostError, MarshalError,
    TimeoutError, MemoryLimitError, InvalidHandleError,
)

def test_acceptance():
    with Runtime(memory_limit=64 * 1024 * 1024) as rt:
        with rt.new_context(timeout=5.0) as ctx:
            # Primitives
            assert ctx.eval("1 + 2") == 3
            assert ctx.eval("'hello'") == "hello"
            assert ctx.eval("true") is True
            assert ctx.eval("null") is None
            assert ctx.eval("undefined") is None
            assert ctx.eval("1.5") == 1.5

            # BigInt
            assert ctx.eval("10n ** 30n") == 10 ** 30

            # Collections
            assert ctx.eval("[1, 2, 3]") == [1, 2, 3]
            assert ctx.eval("({a: 1, b: [2, 3]})") == {"a": 1, "b": [2, 3]}

            # Bytes
            result = ctx.eval("new Uint8Array([1, 2, 3])")
            assert result == b"\x01\x02\x03"

            # Globals (read/write)
            ctx.globals["x"] = 42
            assert ctx.eval("x") == 42
            ctx.globals["data"] = {"n": 100}
            assert ctx.eval("data.n") == 100

            # Host functions: decorator form
            @ctx.function
            def add(a, b):
                return a + b
            assert ctx.eval("add(1, 2)") == 3

            # Host functions: explicit form with name override
            ctx.register("say_hi", lambda name: f"hi {name}")
            assert ctx.eval("say_hi('world')") == "hi world"

            # JS exception → Python
            with pytest.raises(JSError) as excinfo:
                ctx.eval("throw new TypeError('bad thing')")
            assert excinfo.value.name == "TypeError"
            assert excinfo.value.message == "bad thing"
            assert excinfo.value.stack is not None

            # Host exception → JS → Python
            @ctx.function
            def boom():
                raise ValueError("from python")
            with pytest.raises(HostError) as excinfo:
                ctx.eval("boom()")
            assert isinstance(excinfo.value.__cause__, ValueError)

            # JS catching host error
            assert ctx.eval("""
                try { boom(); 'unreachable'; }
                catch (e) { e.name + ': ' + e.message }
            """) == "HostError: from python"

            # Memory limit
            with pytest.raises(MemoryLimitError):
                ctx.eval("let a = []; while(true) a.push(new Array(1e6).fill(0))")

            # Timeout
            with pytest.raises(TimeoutError):
                ctx.eval("while(true){}")

            # Handles
            with ctx.eval_handle("({x: 1, y: 2, add(a, b) { return a + b }})") as obj:
                assert obj.type_of == "object"
                assert obj.get("x").to_python() == 1
                result = obj.call_method("add", 10, 20)
                assert result.to_python() == 30
                result.dispose()

                # allow_opaque keeps the method as a handle
                as_dict = obj.to_python(allow_opaque=True)
                assert as_dict["x"] == 1
                assert hasattr(as_dict["add"], "call")
                as_dict["add"].dispose()

            # Multiple contexts, one runtime
            with rt.new_context() as ctx2:
                ctx2.globals["y"] = "other"
                assert ctx2.eval("y") == "other"
                # Globals don't leak between contexts
                assert ctx.eval("typeof y") == "undefined"

    # After Runtime.__exit__, everything closed, no warnings
```

Additionally: all tests in §11.1 pass; `mypy quickjs_wasm` is clean; `ruff check` is clean; built wheel is `py3-none-any` and installs on a fresh venv.

## 14. Roadmap beyond v0.1

| Version | Scope | Estimate |
|---|---|---|
| 0.2 | Opcode budget limits, configurable WASI (optional clock, optional stdout passthrough for debugging), `default=` hook in MessagePack marshaling, performance profiling pass | 1 week |
| 0.3 | Async: `eval_async`, async host functions, asyncio integration via WASI 0.3 futures if `wasmtime-py` supports by then, else a manual job-pump implementation | 1–2 weeks |
| 0.4 | ES module loader hook (Python-side resolver callback), custom class registration for host objects, `new Uint8Array` zero-copy fast path | 1 week |
| 0.5 | Component-model migration: wasm artifact becomes a component, bindings regenerated from WIT. No user-facing API changes. | 1 week |
| 1.0 | Stability guarantee on the Python API, published benchmarks vs py-mini-racer / pythonmonkey, `langchain-quickjs` middleware package consuming this one | — |

## 15. Open decisions to revisit

- **QuickJS build config**: `CONFIG_BIGNUM=y` is assumed. Confirm compile size is acceptable (~150 KB added). If not, bigint support becomes optional.
- **Scratch starting size**: 64 KB per context. Tune after profiling against realistic agent workloads.
- **ResourceWarning on leaked handles**: keep as warning, or escalate to always-error under a config flag? Default: warning, consistent with stdlib.
- **Python 3.9 support**: dropping costs us nothing and gains `match` and `|`-union types. Staying at 3.10 minimum.
- **Windows wheel testing**: wasmtime-py supports Windows, but WASI filesystem behavior differs. Smoke-test only; real platform coverage when someone files a bug.

## 16. Out of scope, explicit

Not in v0.1, not negotiable during v0.1 implementation:

- Any form of `async def` in the public API
- Loading JS from filesystem (guest has no FS)
- Network access from guest JS
- JSR / npm module resolution
- Node.js globals (`process`, `Buffer`, `require`)
- Browser globals (`window`, `document`, `fetch`)
- WebAssembly inside the JS guest (nested wasm)
- Debugger protocol
- Multi-threading within a runtime
- Sharing handles across processes

## 17. Getting started for an implementer

Suggested order of work:

1. Vendor `quickjs-ng` as a submodule; pin a commit.
2. Write `wasm/shim.c` implementing §6. Build with `build.sh`. Verify it produces a `quickjs.wasm` that at least links without unresolved imports other than `host_call` and `host_interrupt`.
3. Write `quickjs_wasm/_bridge.py`: load the wasm, stub all WASI imports per §9, implement `host_call` and `host_interrupt`. Smoke test: call `qjs_runtime_new`, `qjs_context_new`, `qjs_eval` with `"1+2"`, verify you get a slot back.
4. Write `_msgpack.py` per §8. Unit test round-trip.
5. Write `Runtime`, `Context`, `Handle`, `Globals`, `errors.py` per §7. Fill in the acceptance test one assertion at a time.
6. Flesh out the test files per §11.
7. Wire CI per §4.2 / §11.3.
8. Tag 0.1.0.

If any of §6 / §7 / §8 turns out wrong during implementation, update this spec in the same PR that changes the code. The spec and the code should never disagree.
