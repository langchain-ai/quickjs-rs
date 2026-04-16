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
