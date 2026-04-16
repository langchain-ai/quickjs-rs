"""Exception-class conformance. See spec/implementation.md §7.2, §10.

These are tripwires, not behavioral tests — they check the public
surface matches what §7.2 declares and what ``from quickjs_wasm import
*`` promises. Behavioral tests for the v0.1 errors live in
``test_exceptions.py``; behavioral tests for the v0.2 errors land with
their wiring commits (§17.2 steps 5, 7, 9).

Fail modes these tests catch:

- An error class was accidentally renamed or removed.
- A class was added to ``errors.py`` but not re-exported from the
  top-level package.
- A class was reparented onto the wrong base (e.g. v0.2's
  ``HostCancellationError`` gaining a ``JSError`` subclass by drift —
  the spec says it's a direct ``QuickJSError``, with JS-side name
  injection via the shim).
"""

from __future__ import annotations

import quickjs_wasm


def test_v01_errors_exist_and_subclass_quickjserror() -> None:
    """Locks in the v0.1 hierarchy. §7.2 v0.1 surface."""
    from quickjs_wasm import (
        HostError,
        InterruptError,
        InvalidHandleError,
        JSError,
        MarshalError,
        MemoryLimitError,
        QuickJSError,
        TimeoutError,
    )

    assert issubclass(QuickJSError, Exception)
    assert issubclass(JSError, QuickJSError)
    assert issubclass(HostError, JSError)  # §10.2
    assert issubclass(MarshalError, QuickJSError)
    assert issubclass(InterruptError, QuickJSError)
    assert issubclass(TimeoutError, InterruptError)  # §10.1
    assert issubclass(MemoryLimitError, QuickJSError)
    assert issubclass(InvalidHandleError, QuickJSError)


def test_v02_host_cancellation_error_is_quickjserror_subclass() -> None:
    """§7.2: HostCancellationError extends QuickJSError directly — not
    JSError, not InterruptError. The JS-side appearance as an error
    with ``name == "HostCancellationError"`` is a string-literal
    injection by the shim, not a reflection of the Python hierarchy
    (see §10.3 clarification commit)."""
    from quickjs_wasm import HostCancellationError, JSError, QuickJSError

    assert issubclass(HostCancellationError, QuickJSError)
    # Explicitly not a JSError subclass: HostCancellationError is a
    # bridge-layer signal, not a JS-thrown exception. A drift here would
    # be a spec violation.
    assert not issubclass(HostCancellationError, JSError)


def test_v02_concurrent_eval_error_is_quickjserror_subclass() -> None:
    """§7.2: ConcurrentEvalError extends QuickJSError. Covers both the
    "second eval_async while one is in flight" case (§7.4) and the
    "sync eval hit an async host call" case (§7.4 / §10.3)."""
    from quickjs_wasm import ConcurrentEvalError, QuickJSError

    assert issubclass(ConcurrentEvalError, QuickJSError)


def test_v02_deadlock_error_is_quickjserror_subclass() -> None:
    """§7.2 / §10.3: DeadlockError extends QuickJSError. Raised by
    eval_async when a top-level Promise is pending and no async work
    is in flight to settle it."""
    from quickjs_wasm import DeadlockError, QuickJSError

    assert issubclass(DeadlockError, QuickJSError)


def test_all_declared_exports_importable_from_top_level() -> None:
    """``from quickjs_wasm import *`` should surface every class listed
    in ``__all__``. Guards against the "added to errors.py but forgot
    to re-export" footgun."""
    for name in quickjs_wasm.__all__:
        assert hasattr(quickjs_wasm, name), (
            f"__all__ claims {name!r} but it's not on the top-level package"
        )


def test_v02_error_classes_in_all() -> None:
    """Explicit check that the three v0.2 additions made it into __all__.
    Redundant with the previous test once a class exists, but catches
    the case where ``__all__`` was updated before the class was imported
    into __init__.py (or vice versa)."""
    for name in ("HostCancellationError", "ConcurrentEvalError", "DeadlockError"):
        assert name in quickjs_wasm.__all__, f"{name} missing from __all__"


def test_error_classes_have_docstrings() -> None:
    """IDE hover is the user's discovery surface — a class with no
    docstring is a silent spec regression. §7.2's in-spec class
    definitions are the authoritative wording; the classes in
    errors.py should mirror them."""
    from quickjs_wasm import (
        ConcurrentEvalError,
        DeadlockError,
        HostCancellationError,
    )

    for cls in (HostCancellationError, ConcurrentEvalError, DeadlockError):
        assert cls.__doc__, f"{cls.__name__} has no docstring"
        assert len(cls.__doc__.strip()) > 20, (
            f"{cls.__name__} docstring is suspiciously short"
        )


def test_v02_error_classes_are_raisable_and_catchable() -> None:
    """Final tripwire: the classes actually behave like exceptions.
    Catches a "class defined but something broke __init__" regression."""
    from quickjs_wasm import (
        ConcurrentEvalError,
        DeadlockError,
        HostCancellationError,
        QuickJSError,
    )

    for cls in (HostCancellationError, ConcurrentEvalError, DeadlockError):
        try:
            raise cls(f"test {cls.__name__}")
        except QuickJSError as caught:
            assert isinstance(caught, cls)
            assert cls.__name__ in str(caught)
