"""quickjs-rs: sandboxed JavaScript execution for Python.

See spec/implementation.md §7 for the public API.
"""

from quickjs_rs._msgpack import UNDEFINED, Undefined
from quickjs_rs.context import Context
from quickjs_rs.errors import (
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
from quickjs_rs.handle import Handle
from quickjs_rs.runtime import Runtime

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
