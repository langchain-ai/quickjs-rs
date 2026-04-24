"""quickjs-rs: sandboxed JavaScript execution for Python.

See README.md section 7 for the previous implementation public API and
README.md section 3 for the previous implementation additions (ModuleScope).
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
from quickjs_rs.handle import Handle
from quickjs_rs.modules import ModuleScope
from quickjs_rs.runtime import Runtime
from quickjs_rs.threading import ThreadWorker

__version__ = "0.4.0.dev0"

# Public API surface. previous implementation names (Runtime, Context, Handle, errors,
# Undefined) ship at tag previous implementation. previous implementation adds ModuleScope here; the
# runtime wiring (Runtime.install, module=True eval) lands in later
# steps of README.md section 10.
__all__ = [
    "Runtime",
    "Context",
    "Handle",
    "ModuleScope",
    "ThreadWorker",
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
