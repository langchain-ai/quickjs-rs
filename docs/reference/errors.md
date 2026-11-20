# Errors

Every error quickjs-rs raises on purpose is a `QuickJSError`, with two exceptions:

- An uncaught host-function failure raises your original Python exception.
- Deep recursion raises `wasmtime.Trap`. See [resource limits](../concepts/resource-limits.md#deep-recursion).

A script error becomes a `JSError` with the JavaScript `name`, `message`, and `stack`:

```python
from quickjs_rs import JSError, Runtime

with Runtime() as rt, rt.new_context() as ctx:
    try:
        ctx.eval("null.x")
    except JSError as e:
        print(e.name, "|", e.message)
        #> TypeError | cannot read property 'x' of null

    try:
        ctx.eval("throw 42")
    except JSError as e:
        print(e.name, "|", e.message)
        #> Error | 42
```

A thrown value that is not an `Error` is named `Error`, with its string value as the message.

## Hierarchy

```text
QuickJSError
├── JSError                 name, message, stack
│   └── HostError
├── MarshalError
├── InterruptError
│   └── TimeoutError
├── MemoryLimitError
├── InvalidHandleError
├── HostCancellationError
├── ConcurrentEvalError
└── DeadlockError
```

`quickjs_rs.TimeoutError` is not the built-in `TimeoutError`. Import it from `quickjs_rs`.

| Exception | Raised when |
| --- | --- |
| `JSError` | A script throws and nothing catches it. |
| `HostError` | A script throws its own error named `HostError`. Its `__cause__` is the last host exception, if any. |
| `MarshalError` | A value cannot cross: an unsupported Python type, a non-`str` dict key, a JavaScript function, symbol, or promise in a result, or nesting past depth 128. |
| `TimeoutError` | The eval's deadline passed while JavaScript was running. |
| `InterruptError` | The engine was interrupted before the deadline. This is not expected in normal use. |
| `MemoryLimitError` | An allocation failed against `memory_limit`, and the script did not catch the error. |
| `InvalidHandleError` | A handle was used after `dispose()`, after its context closed, or with another context. |
| `HostCancellationError` | Rejects pending host calls in JavaScript when the task running `eval_async` is cancelled. See [host functions](../concepts/host-functions.md#cancellation). |
| `ConcurrentEvalError` | A sync eval called an async host function, or a second async eval started on a busy context. |
| `DeadlockError` | An async eval is waiting on a promise that nothing can settle. |

Some built-in Python exceptions come from specific calls:

- `ValueError` from `restore_snapshot`, for a bad magic, format version, build ID, size, or stack pointer.
- `TypeError` from `restore_snapshot` for an argument that is not a `Snapshot`, and from registration when quickjs-rs
  cannot tell whether a function is async.
- `NotImplementedError` from `create_snapshot` after a `module=True` eval.

## How JavaScript errors are classified

When a JavaScript error reaches an eval, quickjs-rs looks at its `name` and `message`:

| `name` | `message` | Raised |
| --- | --- | --- |
| `HostError` | `Host function failed`, with a host exception recorded | The original Python exception |
| `HostError` | Anything else | `HostError` |
| `InternalError` | Contains `interrupted`, deadline passed | `TimeoutError` |
| `InternalError` | Contains `interrupted`, deadline not passed | `InterruptError` |
| `InternalError` | Contains `out of memory` | `MemoryLimitError` |
| Anything else | | `JSError` |

Because this goes by name and message, a script can pick its own exception class:

```python
from quickjs_rs import MemoryLimitError, Runtime

with Runtime() as rt, rt.new_context() as ctx:
    try:
        ctx.eval("throw { name: 'InternalError', message: 'out of memory' }")
    except MemoryLimitError:
        print("not really out of memory")
        #> not really out of memory
```

Do not make security decisions on the exception class alone.
