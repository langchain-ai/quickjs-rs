"""Memory, stack, and timeout limits. See README.md."""

from __future__ import annotations

import pytest

from quickjs_rs import JSError, MemoryLimitError, Runtime, TimeoutError


def test_memory_limit_trips_with_runaway_allocation() -> None:
    """JS_ATOM_out_of_memory surfaces as MemoryLimitError once JS
    pushes heap usage past the limit set by JS_SetMemoryLimit."""
    with Runtime(memory_limit=8 * 1024 * 1024) as rt:
        with rt.new_context() as ctx:
            with pytest.raises(MemoryLimitError):
                ctx.eval(
                    "let a = []; while(true) a.push(new Array(1e6).fill(0))"
                )


def test_timeout_terminates_infinite_loop() -> None:
    """wall-clock deadline in host_interrupt kicks QuickJS out of
    an infinite loop within the configured budget."""
    with Runtime() as rt:
        with rt.new_context(timeout=0.2) as ctx:
            with pytest.raises(TimeoutError):
                ctx.eval("while(true){}")


def test_stack_overflow_is_jserror_not_memory() -> None:
    """deep recursion trips JS_ThrowStackOverflow, which is a
    separate path from OOM. The result should be a plain JSError
    (InternalError name), not a MemoryLimitError."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises(JSError) as excinfo:
                ctx.eval("function r(){ return r(); } r()")
            # quickjs-ng reports this as InternalError with a
            # "stack overflow" message; explicitly assert it did NOT
            # match the OOM marker so routing stays correct.
            assert not isinstance(excinfo.value, MemoryLimitError)


def test_timeout_resets_between_evals() -> None:
    """The deadline is per-eval; a subsequent quick eval after a timed-out
    one must not inherit the expired budget."""
    with Runtime() as rt:
        with rt.new_context(timeout=0.2) as ctx:
            with pytest.raises(TimeoutError):
                ctx.eval("while(true){}")
            # Fresh eval on the same context starts with a new 0.2 s
            # budget and finishes immediately.
            assert ctx.eval("1 + 2") == 3


def test_memory_limit_isolated_per_runtime() -> None:
    """memory limit is Runtime-scoped. One runtime hitting its limit
    must not poison a sibling runtime in the same process."""
    with Runtime(memory_limit=8 * 1024 * 1024) as rt1:
        with rt1.new_context() as ctx1:
            with pytest.raises(MemoryLimitError):
                ctx1.eval(
                    "let a = []; while(true) a.push(new Array(1e6).fill(0))"
                )

    with Runtime(memory_limit=64 * 1024 * 1024) as rt2:
        with rt2.new_context() as ctx2:
            assert ctx2.eval("1 + 2") == 3
