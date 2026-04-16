"""quickjs-wasm: sandboxed JavaScript execution for Python.

See spec/implementation.md §7 for the public API.
"""

from quickjs_wasm._msgpack import UNDEFINED, Undefined
from quickjs_wasm.context import Context
from quickjs_wasm.errors import (
    ConcurrentEvalError,
    DeadlockError,
    HostCancellationError,
    HostError,
    InterruptError,
    InvalidHandleError,
    JSError,
    MarshalError,
    MemoryLimitError,
    QuickJSError,
    TimeoutError,
)
from quickjs_wasm.handle import Handle
from quickjs_wasm.runtime import Runtime

__version__ = "0.2.0"

__all__ = [
    "Runtime",
    "Context",
    "Handle",
    "Undefined",
    "UNDEFINED",
    "QuickJSError",
    "JSError",
    "HostError",
    "MarshalError",
    "InterruptError",
    "MemoryLimitError",
    "TimeoutError",
    "InvalidHandleError",
    # v0.2 additions
    "HostCancellationError",
    "ConcurrentEvalError",
    "DeadlockError",
]
