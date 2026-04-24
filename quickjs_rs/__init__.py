"""quickjs-rs: JavaScript execution for Python."""

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

__version__ = "0.1.0.dev0"

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
