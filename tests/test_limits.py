"""Memory, stack, and timeout limits. See README.md."""

from __future__ import annotations

import subprocess
import sys

import pytest

from quickjs_rs import JSError, MemoryLimitError, Runtime, TimeoutError


def test_memory_limit_trips_with_runaway_allocation() -> None:
    """JS_ATOM_out_of_memory surfaces as MemoryLimitError once JS
    pushes heap usage past the limit set by JS_SetMemoryLimit."""
    with Runtime(memory_limit=8 * 1024 * 1024) as rt:
        with rt.new_context() as ctx:
            with pytest.raises(MemoryLimitError):
                ctx.eval("let a = []; while(true) a.push(new Array(1e6).fill(0))")


def test_memory_limit_counts_many_small_allocations() -> None:
    """The limit counts real allocation sizes, so many small strings trip it
    too, not just one allocation larger than the limit."""
    with Runtime(memory_limit=8 * 1024 * 1024) as rt:
        with rt.new_context() as ctx:
            with pytest.raises(MemoryLimitError):
                ctx.eval("let a = []; for (let i = 0; i < 1000; i++) a.push('x'.repeat(50000) + i)")


def test_timeout_terminates_infinite_loop() -> None:
    """wall-clock deadline (checked in the guest) kicks QuickJS out of
    an infinite loop within the configured budget."""
    with Runtime() as rt:
        with rt.new_context(timeout=0.2) as ctx:
            with pytest.raises(TimeoutError):
                ctx.eval("while(true){}")


_ASYNC_EXC_DURING_BUSY_EVAL = """
import ctypes, threading, time
from quickjs_rs import Runtime

class Injected(BaseException):
    pass

with Runtime() as rt, rt.new_context(timeout=10) as ctx:
    caught = []

    def run():
        try:
            ctx.eval("const end = Date.now() + 1500; while (Date.now() < end) {}")
        except Injected:
            caught.append(True)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.5)
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(t.ident), ctypes.py_object(Injected))
    t.join()
    assert caught
"""


def test_async_exception_during_busy_eval_does_not_crash() -> None:
    """An exception raised into a thread mid-eval (Temporal's deadlock
    detector, Ctrl-C) used to land in a host callback's entry and segfault.
    The guest checks the deadline itself, so busy JS runs no host code and
    the exception surfaces after the eval returns.
    Runs in a subprocess so a regression fails the test, not the suite."""
    proc = subprocess.run(
        [sys.executable, "-c", _ASYNC_EXC_DURING_BUSY_EVAL], capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr.decode()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "UPSTREAM GAP (quickjs-ng #774): the wasi build compiles OUT QuickJS's "
        "own stack check (`#if defined(__wasi__)` sets stack_limit=0), so deep "
        "JS recursion exhausts the wasm shadow stack and HARD-TRAPS "
        "('wasm trap: call stack exhausted') before QuickJS can throw a "
        "catchable InternalError. `set_max_stack_size` has no effect (verified "
        "down to 64KB). Catchable stack overflow needs a vendored C patch "
        "restoring the wasi stack check"
    ),
)
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
                ctx1.eval("let a = []; while(true) a.push(new Array(1e6).fill(0))")

    with Runtime(memory_limit=64 * 1024 * 1024) as rt2:
        with rt2.new_context() as ctx2:
            assert ctx2.eval("1 + 2") == 3
