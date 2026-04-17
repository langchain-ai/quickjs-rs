"""quickjs-rs: sandboxed JavaScript execution for Python.

See spec/implementation.md §7 for the public API.
"""

from quickjs_rs._engine import UNDEFINED, Undefined
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
from quickjs_rs.runtime import Runtime

__version__ = "0.3.0.dev0"

# Public API surface is built up step-by-step through phase 1. Context,
# Handle, Undefined, and the full §7 surface land with their respective
# steps (see spec/implementation.md §15). `__all__` is the source of
# truth for "what's ready"; tests that import names not here will
# ImportError cleanly — that's the phase-0 "fail cleanly" contract
# continuing into phase 1 until the relevant step lands.
__all__ = [
    "Runtime",
    "Context",
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
    "HostCancellationError",
    "ConcurrentEvalError",
    "DeadlockError",
]
