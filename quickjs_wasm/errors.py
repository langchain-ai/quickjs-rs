"""Exception hierarchy. See spec/implementation.md §10."""

from __future__ import annotations


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

    def __init__(self, name: str, message: str, stack: str | None = None) -> None:
        super().__init__(f"{name}: {message}")
        self.name = name
        self.message = message
        self.stack = stack


class HostError(JSError):
    """A Python exception from a registered host function that escaped back to Python.

    ``__cause__`` is the original Python exception.
    """


class MarshalError(QuickJSError):
    """A value could not be marshaled (function in eval result, circular ref)."""


class InterruptError(QuickJSError):
    """JS execution was interrupted by the host."""


class TimeoutError(InterruptError):  # noqa: A001 — intentional shadow; see §10
    """The context's timeout elapsed during execution."""


class MemoryLimitError(QuickJSError):
    """The runtime's memory limit was exceeded."""


class InvalidHandleError(QuickJSError):
    """A Handle was used after dispose() or across contexts."""


# v0.2 additions. See spec/implementation.md §7.2, §10.3.


class HostCancellationError(QuickJSError):
    """The enclosing asyncio task was cancelled during ``eval_async``.

    Surfaces in JS as an error with ``.name == "HostCancellationError"``
    that JS code can catch and recover from (absorption). If uncaught in
    JS, ``eval_async`` re-raises ``asyncio.CancelledError`` to the caller.

    The JS-side name is a string literal injected by the shim's
    cancellation-encoding path — same pattern as ``HostError`` in §10.2.
    The Python class name matches by convention; renaming either side
    requires keeping both in sync.
    """


class ConcurrentEvalError(QuickJSError):
    """Concurrent eval violation. Two cases:

    1. A second ``eval_async`` started on a context that already has one
       in flight.
    2. Sync ``eval`` encountered an async host call during execution.

    Use separate contexts for concurrent workloads; use ``eval_async``
    when any registered host function is async.
    """


class DeadlockError(QuickJSError):
    """``eval_async`` detected a pending top-level Promise with no async
    work in flight to settle it.

    Typical causes:

    - A registered function that should have been async was registered
      sync (auto-detection misidentified a wrapped callable — pass
      ``is_async=True`` to ``ctx.register`` explicitly).
    - A user-written JS Promise that never resolves
      (``new Promise(() => {})`` with no resolver capture).
    - Logic bug in evaluated code (forgot to call ``resolve()``, etc.).
    """
