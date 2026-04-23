"""quickjs-rs: sandboxed JavaScript execution for Python.

See spec/implementation.md §7 for the v0.3 public API and
spec/module-loading.md §3 for the v0.4 additions (ModuleScope).
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

__version__ = "0.4.0.dev0"

# Public API surface. v0.3 names (Runtime, Context, Handle, errors,
# Undefined) ship at tag v0.3.0. v0.4 adds ModuleScope here; the
# runtime wiring (Runtime.install, module=True eval) lands in later
# steps of spec/module-loading.md §10.
__all__ = [
    "Runtime",
    "Context",
    "Handle",
    "ModuleScope",
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
