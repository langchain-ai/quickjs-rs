# quickjs-wasm implementation spec

Version: 0.2.0 planning (v0.1.0-rc1 soaking; v0.1 §13.1 acceptance green)
Status: v0.1 complete, v0.2 in planning
Companion docs: `quickjs.wit` (interface contract), `design.md` (design rationale)

## 1. Project overview

`quickjs-wasm` is a Python library for safely executing untrusted JavaScript from a Python host, modeled on `quickjs-emscripten` but hosted directly from Python via a WASI build of QuickJS. Primary consumer: interpreter middleware for Python agent frameworks (langgraph, deepagents). Distribution: PyPI package, pure-Python wheel, bundles a single architecture-independent `quickjs.wasm`.

**Name**: `quickjs-wasm` (PyPI) / `quickjs_wasm` (import).

**License**: MIT, matching QuickJS upstream.

**Python version**: 3.11+. Uses `match` statements, `asyncio.TaskGroup`, and `asyncio.timeout` (both landed in 3.11).

**v0.1 scope (complete, rc1 soaking)**: synchronous eval, globals, handles, primitives + structured values, sync host functions, memory/timeout limits, JS↔Python error propagation. See §13.1 for acceptance criteria.

**v0.2 scope**: async `eval_async` with top-level `await` in module mode, async Python host functions callable from JS, cancellation propagation, `Handle.await_promise`. asyncio-only — trio/curio users can bridge via their own adapters. See §13.2 for acceptance criteria.

**Explicit non-goals for v0.2**: ES module loading from disk, host classes with prototypes, debugger, SharedArrayBuffer, component-model packaging, Python-to-JS async calls (calling a JS async function from Python and awaiting its Promise without going through `eval_async` — workaround: drive via `Handle.await_promise`). See §14 for roadmap.

## 2. Architecture

Three layers, bottom up:

1. **`quickjs.wasm`** — QuickJS compiled with WASI-SDK plus a C shim (`shim.c`) that exposes QuickJS's API as wasm exports and declares wasm imports for host callbacks. Roughly 1 MB gzipped. Architecture-independent; ships verbatim in the Python wheel.

2. **`quickjs_wasm._bridge`** — Python module that loads `quickjs.wasm` via `wasmtime-py`, wires WASI (denied by default: no FS, no net, no clock, no stdio, no env), manages the host function registry, dispatches host calls. Direct wrapper over wasmtime bindings; no user-facing API.

3. **`quickjs_wasm`** (top-level) — user-facing Python API. `Runtime`, `Context`, `Handle`, error types, value marshaling.

The WIT file `quickjs.wit` is the authoritative interface definition between (1) and (2). v0.1 implements the shape by hand using raw WASI because `wasmtime-py`'s component-model bindings are still maturing; the WIT exists as spec documentation and as the migration target for v0.5.

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
│   ├── test_globals.py
│   ├── test_async.py               # v0.2
│   └── test_async_host_functions.py  # v0.2
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
| wasmtime-py | 27+ | Component support not required for v0.1/v0.2 |
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
python -m build                     # → dist/quickjs_wasm-0.2.0-py3-none-any.whl

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
- **host-call** is the single dispatch point for registered sync host functions; takes a fn-id and argument list, returns value or error.
- **host-call-async** (v0.2) dispatches async host calls; the host returns an opaque pending-id immediately and later resolves/rejects the JS Promise via `qjs_promise_resolve` / `qjs_promise_reject`.
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

If the exception value is not an object with a `.name` property (i.e. the JS side did `throw 'x'` or `throw 42`), `qjs_exception_to_msgpack` encodes `name = "Error"` and `message = ToString(exception)`; `stack` is `null`. §10.1 covers how this surfaces on the Python side.

The scratch buffer is per-context, grows in place via `realloc` when a value exceeds current capacity, and starts at 64 KB. There is no separate malloc-and-return path for large values — the single ownership model (scratch owned by shim, invalidated on next marshaling call on this context) applies uniformly. Callers never free marshaling output.

```c
/* Type inspection (does not traverse). Returns a qjs_value_kind. */
uint32_t qjs_type_of(uint32_t ctx, uint32_t slot);
bool     qjs_is_promise(uint32_t ctx, uint32_t slot);
/* 0 = pending, 1 = fulfilled, 2 = rejected, -1 = not a promise */
int32_t  qjs_promise_state(uint32_t ctx, uint32_t slot);

/* v0.2: Promise settlement inspection and resolution.
 *
 * qjs_promise_result returns the resolved value (if fulfilled) or the
 * rejection reason (if rejected) as a slot. On a pending promise, returns
 * negative status — call qjs_promise_state first.
 *
 * qjs_promise_resolve / qjs_promise_reject are used by the host to settle
 * a promise created for an async host call. pending_id is the opaque
 * identifier returned from host_call_async. The value is passed as
 * msgpack bytes in guest memory; the shim decodes it into a JSValue and
 * calls the Promise's internal resolve/reject.
 *
 * Calling resolve or reject twice on the same pending_id, or on a
 * pending_id the shim has already cleaned up (e.g. because the containing
 * runtime was interrupted or the promise was garbage-collected on the JS
 * side), returns negative status. The host is expected to treat this as
 * a benign no-op unless it was a logic error.
 */
int32_t qjs_promise_result(uint32_t ctx, uint32_t promise_slot, uint32_t *out_slot);
int32_t qjs_promise_resolve(uint32_t ctx, uint32_t pending_id,
                            uint32_t value_msgpack_ptr, uint32_t value_msgpack_len);
int32_t qjs_promise_reject(uint32_t ctx, uint32_t pending_id,
                           uint32_t reason_msgpack_ptr, uint32_t reason_msgpack_len);

/* Host function registration.
 * Creates a JS global `name` that, when called, dispatches to the host.
 *
 * is_async: 0 = sync (dispatches through host_call, returns value directly)
 *           1 = async (creates a JS Promise, dispatches through host_call_async,
 *               settles via qjs_promise_resolve / qjs_promise_reject later).
 */
int32_t qjs_register_host_function(uint32_t ctx,
                                   uint32_t name_ptr, uint32_t name_len,
                                   uint32_t fn_id, uint32_t is_async);

/* Guest memory allocation for host-provided buffers.
 * qjs_malloc returns a guest pointer the host can write into, then passes
 * that pointer back into eval/set_prop/etc. The host must qjs_free it
 * (unless ownership is explicitly transferred, which is documented per
 * function — none transfer in v0.1 or v0.2).
 */
uint32_t qjs_malloc(uint32_t size);
void     qjs_free(uint32_t ptr);
```

### 6.3 Imports (declared by the shim, provided by the Python host)

```c
/* Dispatched when JS calls a sync host-registered function.
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

/* v0.2: Dispatched when JS calls an async host-registered function.
 * The host schedules the real work (e.g. via an asyncio task), returns
 * an opaque pending_id immediately in *out_pending_id. The shim creates
 * a JS Promise and returns it to JS synchronously; the host later
 * settles the promise via qjs_promise_resolve or qjs_promise_reject
 * keyed by that pending_id.
 *
 * Pending IDs are unique per context and monotonically increasing.
 * A return status other than 0 indicates the host rejected the call
 * synchronously (e.g. the function doesn't exist, args couldn't be
 * decoded). In that case no Promise is created and no settlement
 * is expected.
 */
__attribute__((import_name("host_call_async")))
int32_t host_call_async(uint32_t fn_id,
                        uint32_t args_ptr, uint32_t args_len,
                        uint32_t *out_pending_id);

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
- **v0.2** Pending IDs returned from `host_call_async` are monotonically increasing per context and unique. The shim-side map from `pending_id → (resolve, reject)` is cleaned up when either resolver fires. Calling `qjs_promise_resolve` or `qjs_promise_reject` with an already-settled or unknown `pending_id` returns negative status without side effects.
- **v0.2** `qjs_promise_result` on a pending promise returns negative status; callers must check `qjs_promise_state` first.
- **v0.2** When a runtime's interrupt fires while async host calls are in flight, the shim does not attempt to cancel those host calls — cancellation is a host-side concern. The shim does drop its side of the pending map entries for the interrupted context, so late `qjs_promise_resolve` / `qjs_promise_reject` calls from the host become benign no-ops.

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
    # v0.2:
    HostCancellationError, ConcurrentEvalError, DeadlockError,
)

__version__ = "0.2.0"
__all__ = [
    "Runtime", "Context", "Handle",
    "QuickJSError", "JSError", "HostError", "MarshalError",
    "InterruptError", "MemoryLimitError", "TimeoutError", "InvalidHandleError",
    "HostCancellationError", "ConcurrentEvalError", "DeadlockError",
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
        MemoryLimitError if the runtime memory limit is hit,
        ConcurrentEvalError if execution encounters an async host call
        (use eval_async instead).
        """

    def eval_handle(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> "Handle": ...

    # v0.2: async API
    async def eval_async(
        self,
        code: str,
        *,
        module: bool = True,           # defaults to top-level-await-enabled script
        strict: bool = False,
        filename: str = "<eval>",
        timeout: float | None = None,  # per-call override of context cumulative budget
    ) -> Any:
        """Evaluate asynchronously, driving pending jobs until any top-level
        Promise settles. Required when the evaluated code awaits, or when
        any registered async host function fires during execution.

        ``module=True`` (default) compiles with QuickJS's script mode plus
        the top-level-await flag (JS_EVAL_TYPE_GLOBAL | JS_EVAL_FLAG_ASYNC),
        which combines script-mode completion-value semantics (a bare
        expression yields its result) with top-level await. The kwarg is
        named ``module`` to match the JS-developer concept; the underlying
        mechanism is technically a global-mode script with the async flag.
        ``module=False`` compiles as plain script mode (no top-level await).

        See §7.4 for the detailed execution model.

        Raises the same exceptions as eval(), plus:
        - HostCancellationError, via the enclosing cancel scope
        - DeadlockError if the top-level promise has no async work in flight
        - ConcurrentEvalError on parallel eval_async against the same context
        """

    async def eval_handle_async(
        self,
        code: str,
        *,
        module: bool = True,
        strict: bool = False,
        filename: str = "<eval>",
        timeout: float | None = None,
    ) -> "Handle":
        """Like eval_async, but returns a Handle instead of marshaling out."""

    @property
    def globals(self) -> "Globals": ...

    @overload
    def function(self, fn: Callable[..., Any]) -> Callable[..., Any]: ...
    @overload
    def function(self, *, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        is_async: bool | None = None,  # v0.2: None = auto-detect via inspect
    ) -> None:
        """Register a Python callable as a JS global function named `name`.

        is_async:
            None (default) — auto-detect via inspect.iscoroutinefunction
            True / False  — explicit override (for wrapped callables where
                            detection fails)
        """

    @property
    def timeout(self) -> float:
        """Timeout semantics:

        - sync eval: per-call, starts at eval entry, clears on return.
        - eval_async: cumulative across all eval_async calls on this
          context, starting from context creation. Per-call override via
          the timeout= kwarg on eval_async itself.
        """
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

    # v0.2: real implementation (was NotImplementedError stub in v0.1)
    async def await_promise(self, *, timeout: float | None = None) -> "Handle":
        """Drive pending jobs until this promise settles, then return a
        handle to the resolved value (or raise on rejection).

        Must be called inside an async context. Respects the enclosing
        cancel scope. If the handle is not a promise, returns self
        unchanged (idiomatic with chained handle ops).
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

# v0.2 additions

class HostCancellationError(QuickJSError):
    """The enclosing asyncio task was cancelled during eval_async.

    Surfaces in JS as an error with name='HostCancellationError' that JS
    code can catch and recover from. If uncaught in JS, eval_async
    re-raises asyncio.CancelledError to the caller.
    """

class ConcurrentEvalError(QuickJSError):
    """Concurrent eval violation. Two cases:
    1. A second eval_async started on a context that already has one in flight.
    2. Sync eval encountered an async host call during execution.

    Use separate contexts for concurrent workloads; use eval_async when
    any registered host function is async.
    """

class DeadlockError(QuickJSError):
    """eval_async detected a pending top-level Promise with no async work
    in flight to settle it. Typical causes: a registered function that
    should have been async was registered sync, or a user-written JS
    Promise that never resolves.
    """
```

### 7.3 Behavioral specifics

**Context manager semantics.** `Runtime.__exit__` closes all contexts created by it. `Context.__exit__` disposes all outstanding handles and closes the context. Calling `close()` twice is a no-op.

**Handle ownership.** Handles are tied to the context that created them. Using a handle from context A in a call on context B raises `InvalidHandleError`. When a handle is garbage-collected without explicit dispose, `__del__` calls `dispose()` and emits a `ResourceWarning` — this is Python convention for leaked resources (see `warnings.filterwarnings("error", category=ResourceWarning)` in tests).

**Global mapping proxy.** `ctx.globals["x"] = 1` performs a single `set-global`. Reads perform `get-global` each time (no caching). `ctx.globals.get_handle("x")` returns a handle instead of a marshaled value.

**Function registration.** `@ctx.function` uses `fn.__name__` unless overridden. Keyword-only `name=` when used as `@ctx.function(name="myFn")`. Registered functions see Python-native args (already marshaled from MessagePack) and return Python-native values (marshaled back). Type coercion follows §8.

Sync vs async is auto-detected from the callable via `inspect.iscoroutinefunction`. `ctx.register(name, fn, is_async=True/False)` overrides when auto-detection picks wrong (e.g. wrapped callables, manually-constructed awaitables).

**Timeout.** For sync `eval`, timeout is per-call: wall-clock seconds from the start of the call, cleared on return. For `eval_async`, timeout is *cumulative* across all `eval_async` calls on the same context (starting when the context is created), unless an explicit `timeout=` kwarg is passed — in which case that value applies only to the current call. See §7.4.

`host_interrupt` implementation compares `time.monotonic()` to the relevant deadline, returns 1 if exceeded.

**Memory limit.** Per-runtime. Exceeding it causes the current JS operation to raise `MemoryLimitError` in Python (QuickJS returns an out-of-memory exception, which we intercept and rewrap).

**Async execution summary.** See §7.4 for the full model.

### 7.4 Async execution model

#### Library stance

`quickjs_wasm` uses stdlib `asyncio` directly. No anyio dependency, no trio compatibility out of the box — trio or curio users who need this can bridge via their own adapters (`trio-asyncio` etc.); we don't ship that glue.

Internals rely on 3.11+ primitives: `asyncio.TaskGroup` for structured concurrency around in-flight host calls, `asyncio.timeout` for cumulative-budget enforcement, `asyncio.CancelledError` for cancellation propagation, `asyncio.Event` for host-call completion signaling.

#### Host function detection

`@ctx.function` and `ctx.register(...)` auto-detect via `inspect.iscoroutinefunction(fn)`:

- `async def` functions → registered as async (`is_async=1` in the shim)
- regular `def` functions → registered as sync (`is_async=0`)

Auto-detection fails for some wrapped callables (notably those decorated in ways that don't preserve the `__wrapped__` chain or expose the underlying coroutine). Use `ctx.register(name, fn, is_async=True/False)` to force the mode.

#### JS-side shape

Sync host functions return values directly. Async host functions return `Promise<T>`. From user-written JS, both look like function calls:

```javascript
const n = add(1, 2);                // sync: immediate
const s = await readFile("/x");     // async: awaited
```

Pure JS code that calls only sync host functions doesn't need `await` and runs fine under sync `eval` or async `eval_async`.

#### `eval_async` driving loop

`eval_async` runs the code. If the result is a resolved value (no top-level await, no pending host calls), it marshals and returns immediately. If the result is a pending Promise, it drives the event loop:

1. Run pending jobs via `qjs_runtime_run_pending_jobs` until the job queue is empty.
2. Check the top-level promise state.
3. If `fulfilled`: marshal the resolved value and return.
4. If `rejected`: marshal the rejection reason and raise (via the existing exception extraction path).
5. If `pending` and one or more async host calls are in flight: `await` an internal `asyncio.Event` that signals on the next host-call completion.
6. If `pending` and no host calls are in flight: raise `DeadlockError`.

Async host-call completions (success or failure) call `qjs_promise_resolve` / `qjs_promise_reject` through the shim, then signal the event to wake the driving loop.

#### Concurrency

Only one `eval_async` may run per context at a time. A second concurrent `eval_async` raises `ConcurrentEvalError` immediately. For concurrent JS workloads, use multiple contexts on the same runtime — they share the heap (efficient) but have independent job queues and host-call registries.

Multiple async host calls *in flight simultaneously from a single `eval_async`* are fully supported. `Promise.all([readFile(a), readFile(b), readFile(c)])` from JS produces three concurrent host-call tasks scheduled into the internal task group.

#### Cancellation

`eval_async` respects asyncio cancellation. When the enclosing task is cancelled (via `task.cancel()`, a timeout, or a `TaskGroup` teardown):

1. The driving loop catches `asyncio.CancelledError` at its `await` point.
2. The internal `TaskGroup` cancels all in-flight async host-call tasks.
3. Each corresponding JS Promise is rejected with `HostCancellationError` (via `qjs_promise_reject`).
4. One final `qjs_runtime_run_pending_jobs` runs, so JS `catch` / `finally` handlers execute.
5. If the JS code catches `HostCancellationError` and completes normally, `eval_async` returns normally. Cancellation has been "absorbed" by JS. Note: this means `asyncio.CancelledError` does *not* re-raise in that case — the JS side effectively swallowed it. This is intentional, but users who need cancellation to always propagate should check `asyncio.current_task().cancelling()` after the call.
6. If the JS code does not catch (the common case), `eval_async` re-raises `asyncio.CancelledError`.

The absorption behavior in step 5 is a design choice that prioritizes JS-side cleanup over unconditional propagation. It matches how sync `try/finally` in JS already works in this codebase and avoids surprising users who put cleanup logic in JS. Revisit in §15 if the semantics cause confusion.

#### Timeout

Timeout applies via the same `host_interrupt` mechanism as sync eval, with these differences:

- **Default**: the context's `timeout=` kwarg (at creation) becomes a *cumulative budget* spanning all `eval_async` calls on that context. If total time across calls exceeds the budget, the next interrupt check aborts with `TimeoutError`. This is the right default because async eval is often used in long-running agent loops where each call is short but the total should be bounded.
- **Explicit `timeout=N` on `eval_async`**: applies only to that call. The context budget is not consulted.

Sync `eval` timeout semantics are unchanged — per-call, cleared on return.

Wasmtime epoch interruption (50 ms cadence) remains the backup path for the rare case where QuickJS's interrupt hook somehow doesn't fire (deep C-stack in a QuickJS built-in, etc.).

#### Sync eval + async host functions

If sync `eval` runs JS that calls a registered async host function, the first attempt to resolve the resulting Promise raises `ConcurrentEvalError("cannot drive async host call from sync eval; use eval_async")`. The evaluation fails cleanly; the context is not corrupted and can be reused for subsequent sync or async eval calls. The JS-side Promise is rejected, which means JS code that catches rejection before control returns to sync `eval` could in theory handle this gracefully — but in practice if you're mixing sync eval with async host functions you should switch to `eval_async`.

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

Unmarshalable Python types (sets, custom classes, datetimes, etc.) raise `MarshalError` at the point of marshaling. v0.1/v0.2 do not support a `default` hook; added in v0.3.

`Context.eval(...)` defaults to converting `undefined` → `None`. Set `ctx.preserve_undefined = True` to get the `Undefined` singleton instead.

## 9. Limits and safety defaults

| Limit | Default | Scope | Enforcement |
|---|---|---|---|
| Memory | 64 MB | Runtime | QuickJS `JS_SetMemoryLimit` |
| Stack | 1 MB | Runtime | QuickJS `JS_SetMaxStackSize` |
| Timeout | 5 s | Context | `host_interrupt` wall-clock check; per-call for sync `eval`, cumulative for `eval_async` (overridable per-call) |
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

When JS throws a non-`Error` value (e.g. `throw 'x'` or `throw 42`), the shim coerces the thrown value to a string via `ToString` and surfaces it as `JSError(name='Error', message=<coerced string>, stack=None)`. The JS-side distinction between thrown `Error` instances and thrown primitives is not preserved on the Python side — callers that need to round-trip arbitrary non-Error throws should use `eval_handle` and inspect the thrown value directly.

### 10.2 From Python host functions to JS

When a registered host function raises (sync or async path):

1. The bridge catches the Python exception
2. Encodes it as a JS error record: `{name: "HostError", message: str(exc), stack: traceback}`
3. For sync: returns it via `host_call` with status=1, the shim's host-function wrapper throws it in JS
4. For async: calls `qjs_promise_reject` with the encoded error, the Promise rejects, `await` in JS raises

If that JS error propagates back out to Python uncaught, it's rewrapped as `HostError` with `__cause__` set to the original Python exception.

### 10.3 Async-specific errors

`HostCancellationError` is surfaced when the enclosing asyncio task is cancelled during `eval_async`. It appears to JS as an error with a `.name` property of the string `"HostCancellationError"`, injected by the shim's cancellation-encoding path (same pattern as `HostError` in §10.2 — the Python class name matches the injected string literal by convention, and renaming either side requires keeping both in sync). JS code can catch with try/catch for cleanup. If uncaught in JS, `eval_async` re-raises `asyncio.CancelledError` to the caller.

`ConcurrentEvalError` is raised in two cases:
- A second `eval_async` starts on a context with one already in flight.
- Sync `eval` encounters an async host call during execution.

`DeadlockError` is raised when `eval_async`'s driving loop detects a pending top-level promise with no async work in flight to settle it. Typical causes:
- A registered function that should be async was registered sync (common when auto-detection misidentifies a wrapped callable).
- A user-written JS Promise that never resolves.
- Logic bug in evaluated code (forgot to call `resolve()`, etc.).

### 10.4 Invariant

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

Async tests use `pytest-asyncio` with `asyncio_mode = "auto"` so `async def test_*` functions run without explicit decoration:

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

### 11.1 Test files and what each covers

**`test_primitives.py`** — number, string, bool, null, undefined, bigint, bytes round-trip. JS → Python and Python → JS via globals.

**`test_objects.py`** — plain objects, arrays, nested structures, key insertion order preservation, Uint8Array round-trip.

**`test_host_functions.py`** — register sync function, JS calls it, return values propagate, exceptions propagate. Decorator and explicit `register`. `name=` override. Positional and keyword args (JS has no kwargs — only positional, document this).

**`test_exceptions.py`** — `throw new TypeError()` raises `JSError` with correct `name`/`message`/`stack`. Python exception in host fn raises `HostError` with `__cause__`. JS catching a `HostError` with try/catch and inspecting it.

**`test_limits.py`** — memory limit triggers `MemoryLimitError`. Timeout triggers `TimeoutError`. Stack overflow in JS raises `JSError(name="InternalError")` (not the memory limit).

**`test_handles.py`** — handle lifecycle: dispose, context manager, `__del__` emits `ResourceWarning`, cross-context use raises `InvalidHandleError`, `to_python` traverses correctly, `allow_opaque=True` keeps functions as handles.

**`test_globals.py`** — get/set/contains, `get_handle`, handle-backed assignments.

**`test_async.py`** (v0.2) — top-level `await` in module mode, `eval_async` over pure-sync code (should still work), `eval_handle_async`, promise chains that resolve synchronously, `Handle.await_promise` standalone, context timeout as cumulative budget, per-call timeout override.

**`test_async_host_functions.py`** (v0.2) — async function registration (decorator auto-detect + explicit), `Promise.all` fan-out, mixed sync + async host calls in one eval, cancellation during flight (with and without JS-side catch), DeadlockError cases, ConcurrentEvalError for parallel `eval_async` on same context, sync-eval-with-async-hostfn failure path, async host function that raises.

### 11.2 Integration smoke test

`tests/test_smoke.py` runs the acceptance criteria from §13 end-to-end as a single test, providing a tripwire if anything fundamental regresses. For v0.2, the sync block from §13.1 and the async block from §13.2 both live in this file.

### 11.3 CI matrix

GitHub Actions. Matrix: Python 3.11, 3.12, 3.13 × Linux, macOS, Windows. Async tests run on asyncio via `pytest-asyncio`. Single wasm artifact built once on Linux and cached; tests download and use it. Wheel build/publish on tag.

## 12. Dependencies

`pyproject.toml`:

```toml
[project]
name = "quickjs-wasm"
version = "0.2.0"
requires-python = ">=3.11"
dependencies = [
    "wasmtime>=27.0.0",
    "msgpack>=1.1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov",
    "pytest-asyncio>=0.23",
    "ruff",
    "mypy",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

No other runtime deps. Wheel is `py3-none-any` — platform-independent because wasmtime-py ships native extensions and `quickjs.wasm` is architecture-independent.

## 13. Acceptance criteria

### 13.1 v0.1 acceptance

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

### 13.2 v0.2 acceptance

The §13.1 acceptance test continues to pass unchanged. Additionally, the following runs to completion on asyncio (via `pytest-asyncio` auto mode):

```python
import asyncio
import pytest
from quickjs_wasm import (
    Runtime, DeadlockError, ConcurrentEvalError,
)

async def test_async_acceptance():
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # Auto-detected async host function
            @ctx.function
            async def sleep_ms(n: int) -> str:
                await asyncio.sleep(n / 1000)
                return "slept"

            # Top-level await in module mode
            assert await ctx.eval_async("await sleep_ms(10)") == "slept"

            # Promise.all fan-out, multiple concurrent host calls
            result = await ctx.eval_async("""
                const results = await Promise.all([
                    sleep_ms(5),
                    sleep_ms(10),
                    sleep_ms(15),
                ]);
                results.join(",")
            """)
            assert result == "slept,slept,slept"

            # Mixed sync + async host calls in one eval
            @ctx.function
            def double(n: int) -> int:
                return n * 2

            @ctx.function
            async def slow_double(n: int) -> int:
                await asyncio.sleep(0.001)
                return n * 2

            result = await ctx.eval_async("""
                const a = double(5);              // sync, immediate
                const b = await slow_double(10);  // async, awaited
                a + b
            """)
            assert result == 30

            # The motivating agent-code pattern: readFile + swarm
            captured_reads: list[str] = []

            @ctx.function
            async def readFile(path: str) -> str:
                captured_reads.append(path)
                return "Date: 2024-01-01\nDate: 2024-01-02\nNotDate"

            @ctx.function
            async def swarm(tasks: list, opts: dict) -> dict:
                return {
                    "completed": len(tasks),
                    "failed": 0,
                    "results": [
                        {
                            "id": t["id"],
                            "status": "completed",
                            "result": '{"abbreviation_count": 1}',
                        }
                        for t in tasks
                    ],
                }

            result = await ctx.eval_async("""
                const raw = await readFile("/context.txt");
                const lines = raw.split("\\n").filter(l => l.startsWith("Date:"));
                const summary = await swarm(
                    lines.map((line, i) => ({ id: `t_${i}`, description: line })),
                    { concurrency: 32 }
                );
                let total = 0;
                for (const r of summary.results) {
                    if (r.status === "completed") {
                        total += JSON.parse(r.result).abbreviation_count;
                    }
                }
                total
            """)
            assert result == 2
            assert captured_reads == ["/context.txt"]

            # Cancellation: task.cancel() propagates through eval_async
            task = asyncio.create_task(
                ctx.eval_async("await sleep_ms(10000)")
            )
            await asyncio.sleep(0.01)  # let it start
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # JS catching HostCancellationError and recovering
            # Cancel via asyncio.timeout; JS catches and returns sentinel;
            # eval_async returns normally since cancellation was absorbed
            async with asyncio.timeout(0.02):
                try:
                    caught = await ctx.eval_async("""
                        try {
                            await sleep_ms(10000);
                            "unreachable"
                        } catch (e) {
                            e.name
                        }
                    """)
                    assert caught == "HostCancellationError"
                except asyncio.CancelledError:
                    # Acceptable alternate path: cancellation propagated
                    # before JS catch handler ran. Either outcome is a
                    # valid implementation of §7.4 cancellation.
                    pass

            # DeadlockError: pending promise with no async work
            with pytest.raises(DeadlockError):
                await ctx.eval_async(
                    "new Promise((resolve) => {})",
                    module=False,
                )

            # ConcurrentEvalError: two eval_async at once on same context
            async def first():
                await ctx.eval_async("await sleep_ms(100)")

            async with asyncio.TaskGroup() as tg:
                tg.create_task(first())
                await asyncio.sleep(0.01)  # let first start
                with pytest.raises(ConcurrentEvalError):
                    await ctx.eval_async("1 + 1")

            # Sync eval + async host fn: clean failure
            with pytest.raises(ConcurrentEvalError):
                ctx.eval("sleep_ms(1)")  # returns a Promise sync eval can't drive

            # Handle.await_promise
            p = await ctx.eval_handle_async("Promise.resolve(42)")
            resolved = await p.await_promise()
            assert resolved.to_python() == 42
            resolved.dispose()
            p.dispose()
```

Additionally: tests in `test_async.py` and `test_async_host_functions.py` pass under asyncio; `mypy` and `ruff` clean; wheel installs.

## 14. Roadmap

| Version | Scope | Status |
|---|---|---|
| 0.1 | sync eval, host functions, globals, handles, limits, error propagation | complete (rc1 soaking) |
| 0.2 | async: `eval_async`, async host functions, cancellation, asyncio integration | planning |
| 0.3 | Opcode budget limits, configurable WASI (optional clock, optional stdout passthrough for debugging), `default=` hook in MessagePack marshaling, performance profiling pass, `qjs_own_keys` shim export for allow_opaque perf, proper cycle detection via `qjs_is_same` | |
| 0.4 | ES module loader hook (Python-side resolver callback), custom class registration for host objects, `new Uint8Array` zero-copy fast path, Python-to-JS async calls | |
| 0.5 | Component-model migration: wasm artifact becomes a component, bindings regenerated from WIT. No user-facing API changes. | |
| 1.0 | Stability guarantee on the Python API, published benchmarks vs py-mini-racer / pythonmonkey, `langchain-quickjs` middleware package consuming this one | |

## 15. Open decisions to revisit

- **QuickJS build config**: `CONFIG_BIGNUM=y` is assumed. Confirm compile size is acceptable (~150 KB added). If not, bigint support becomes optional.
- **Scratch starting size**: 64 KB per context. Tune after profiling against realistic agent workloads.
- **ResourceWarning on leaked handles**: keep as warning, or escalate to always-error under a config flag? Default: warning, consistent with stdlib.
- **Python 3.9 support**: dropping costs us nothing and gains `match` and `|`-union types. Staying at 3.10 minimum.
- **Windows wheel testing**: wasmtime-py supports Windows, but WASI filesystem behavior differs. Smoke-test only; real platform coverage when someone files a bug.
- **v0.2 sync eval with async host fns**: currently raises `ConcurrentEvalError` on the first promise-drive attempt. Alternative: eagerly raise at the host_call boundary. Current design picked for simplicity; revisit if users find the error point confusing.
- **v0.2 cumulative timeout for eval_async**: chosen over per-call as the default because long-running agent loops benefit more from a total budget. Revisit if the semantic causes confusion.

## 16. Out of scope, explicit

Not in v0.1 or v0.2:

- Loading JS from filesystem (guest has no FS)
- Network access from guest JS
- JSR / npm module resolution
- Node.js globals (`process`, `Buffer`, `require`)
- Browser globals (`window`, `document`, `fetch`)
- WebAssembly inside the JS guest (nested wasm)
- Debugger protocol
- Multi-threading within a runtime
- Sharing handles across processes
- Python-to-JS async calls (awaiting a JS async function from Python without going through `eval_async`) — workaround exists via `Handle.await_promise`; first-class API deferred to v0.4+

## 17. Getting started for an implementer

### 17.1 v0.1 implementation order (shipped)

1. Vendor `quickjs-ng` as a submodule; pin a commit.
2. Write `wasm/shim.c` implementing §6. Build with `build.sh`. Verify it produces a `quickjs.wasm` that at least links without unresolved imports other than `host_call` and `host_interrupt`.
3. Write `quickjs_wasm/_bridge.py`: load the wasm, stub all WASI imports per §9, implement `host_call` and `host_interrupt`. Smoke test: call `qjs_runtime_new`, `qjs_context_new`, `qjs_eval` with `"1+2"`, verify you get a slot back.
4. Write `_msgpack.py` per §8. Unit test round-trip.
5. Write `Runtime`, `Context`, `Handle`, `Globals`, `errors.py` per §7. Fill in the §13.1 acceptance test one assertion at a time.
6. Flesh out the test files per §11.1.
7. Wire CI per §4.2 / §11.3.
8. Tag 0.1.0.

### 17.2 v0.2 implementation order

1. Install `pytest-asyncio` in dev extras (§12). No new runtime deps — asyncio is stdlib.
2. Extend `shim.c` with the promise exports (`qjs_promise_result`, `qjs_promise_resolve`, `qjs_promise_reject`) and the `host_call_async` import declaration (§6.2, §6.3). Extend `qjs_register_host_function` with the `is_async` parameter and route accordingly. Stub the host side initially; verify the shim builds and `qjs_register_host_function(is_async=1)` correctly creates a Promise-returning JS function.
3. Extend `_bridge.py` with:
   - Host-call-async dispatch (the `host_call_async` import implementation).
   - Per-context pending-ID → `(resolve, reject, cancel_scope)` map.
   - An `asyncio.Event` per eval_async invocation that signals when any pending host call completes.
   - `qjs_promise_resolve` / `qjs_promise_reject` wiring, called from host-call completion callbacks.
4. Add the new error classes to `errors.py` (§7.2). Re-export from `__init__.py`.
5. Implement `Context.eval_async` and `Context.eval_handle_async` with the driving loop described in §7.4.
6. Implement async host function auto-detection in `@ctx.function` and `ctx.register(..., is_async=...)`.
7. Implement cancellation propagation: catch `asyncio.CancelledError` at the driving loop's `await`, cancel the internal `TaskGroup`, reject in-flight promises, final pending-jobs drain, re-raise unless JS absorbed it.
8. Implement `Handle.await_promise` as a real method (was stubbed in v0.1 per §7.2).
9. Implement the sync-eval-with-async-hostfn failure path: sync `eval` detects a pending promise where none was expected and raises `ConcurrentEvalError`.
10. Fill in `test_async.py` and `test_async_host_functions.py` per §11.1 with asyncio primitives.
11. Extend `test_smoke.py` with the §13.2 async acceptance test.
12. Update `CLAUDE.md` to mention async-specific guidance where relevant (e.g. the concurrent-eval invariant, the cumulative-timeout semantic).
13. Tag v0.2.0-rc1 and soak. Tag v0.2.0 after soak passes.

If any of §6 / §7 / §8 turns out wrong during implementation, update this spec in the same PR that changes the code. The spec and the code should never disagree.
