"""Memory, stack, and timeout limits. See spec/implementation.md §9, §11.1."""

from __future__ import annotations

import time

import pytest

from quickjs_rs import JSError, MemoryLimitError, Runtime, TimeoutError


def test_memory_limit_trips_with_runaway_allocation() -> None:
    """§9: JS_ATOM_out_of_memory surfaces as MemoryLimitError once JS
    pushes heap usage past the limit set by JS_SetMemoryLimit."""
    with Runtime(memory_limit=8 * 1024 * 1024) as rt:
        with rt.new_context() as ctx:
            with pytest.raises(MemoryLimitError):
                ctx.eval("let a = []; while(true) a.push(new Array(1e6).fill(0))")


def test_timeout_terminates_infinite_loop() -> None:
    """§7.3: wall-clock deadline in host_interrupt kicks QuickJS out of
    an infinite loop within the configured budget."""
    with Runtime() as rt:
        with rt.new_context(timeout=0.2) as ctx:
            with pytest.raises(TimeoutError):
                ctx.eval("while(true){}")


def test_stack_overflow_is_jserror_not_memory() -> None:
    """§11.1: deep recursion trips JS_ThrowStackOverflow, which is a
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
    """§9: memory limit is Runtime-scoped. One runtime hitting its limit
    must not poison a sibling runtime in the same process."""
    with Runtime(memory_limit=8 * 1024 * 1024) as rt1:
        with rt1.new_context() as ctx1:
            with pytest.raises(MemoryLimitError):
                ctx1.eval("let a = []; while(true) a.push(new Array(1e6).fill(0))")

    with Runtime(memory_limit=64 * 1024 * 1024) as rt2:
        with rt2.new_context() as ctx2:
            assert ctx2.eval("1 + 2") == 3


def test_redos_like_regex_is_bounded_by_timeout() -> None:
    """Threat: catastrophic backtracking ReDoS.

    The timeout interrupt should abort this payload.
    """
    with Runtime() as rt:
        with rt.new_context(timeout=0.05) as ctx:
            with pytest.raises(TimeoutError):
                ctx.eval("const r=/^(a+)+$/; const s='a'.repeat(200000) + 'X'; r.test(s);")


def test_no_builtin_os_or_network_capabilities_by_default() -> None:
    """Threat: sandbox escape via default globals.

    QuickJS here has no Node/Deno host APIs unless Python exposes
    them via registered host functions.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            assert ctx.eval("typeof process") == "undefined"
            assert ctx.eval("typeof require") == "undefined"
            assert ctx.eval("typeof Deno") == "undefined"
            assert ctx.eval("typeof fetch") == "undefined"
            assert ctx.eval("typeof WebSocket") == "undefined"
            assert ctx.eval('typeof Function("return this")().process') == "undefined"


def test_timeout_does_not_preempt_long_running_python_host_function() -> None:
    """Risk MRE: timeout only interrupts JS bytecode, not host Python.

    A blocking host function can run past the JS timeout budget.
    """
    with Runtime() as rt:
        with rt.new_context(timeout=0.05) as ctx:

            @ctx.function
            def sleepy(ms: int) -> int:
                time.sleep(ms / 1000.0)
                return 7

            start = time.monotonic()
            assert ctx.eval("sleepy(200)") == 7
            elapsed = time.monotonic() - start
            assert elapsed >= 0.18
