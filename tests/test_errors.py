"""Exception-class conformance tests."""

from __future__ import annotations

import quickjs_rs


def test_core_error_hierarchy() -> None:
    from quickjs_rs import (
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
    assert issubclass(HostError, JSError)
    assert issubclass(MarshalError, QuickJSError)
    assert issubclass(InterruptError, QuickJSError)
    assert issubclass(TimeoutError, InterruptError)
    assert issubclass(MemoryLimitError, QuickJSError)
    assert issubclass(InvalidHandleError, QuickJSError)


def test_host_cancellation_error_base_class() -> None:
    from quickjs_rs import HostCancellationError, JSError, QuickJSError

    assert issubclass(HostCancellationError, QuickJSError)
    assert not issubclass(HostCancellationError, JSError)


def test_concurrent_eval_error_base_class() -> None:
    from quickjs_rs import ConcurrentEvalError, QuickJSError

    assert issubclass(ConcurrentEvalError, QuickJSError)


def test_deadlock_error_base_class() -> None:
    from quickjs_rs import DeadlockError, QuickJSError

    assert issubclass(DeadlockError, QuickJSError)


def test_all_declared_exports_importable_from_top_level() -> None:
    for name in quickjs_rs.__all__:
        assert hasattr(quickjs_rs, name), (
            f"__all__ claims {name!r} but it's not on the top-level package"
        )


def test_async_error_classes_in_all() -> None:
    for name in ("HostCancellationError", "ConcurrentEvalError", "DeadlockError"):
        assert name in quickjs_rs.__all__, f"{name} missing from __all__"


def test_error_classes_have_docstrings() -> None:
    from quickjs_rs import (
        ConcurrentEvalError,
        DeadlockError,
        HostCancellationError,
    )

    for cls in (HostCancellationError, ConcurrentEvalError, DeadlockError):
        assert cls.__doc__, f"{cls.__name__} has no docstring"
        assert len(cls.__doc__.strip()) > 20, (
            f"{cls.__name__} docstring is suspiciously short"
        )


def test_async_error_classes_are_raisable_and_catchable() -> None:
    from quickjs_rs import (
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
