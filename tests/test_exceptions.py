"""Exception propagation. See README.md."""

from __future__ import annotations

import pytest

from quickjs_rs import HostError, JSError, Runtime


def test_js_thrown_error_surfaces_name_message_stack() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises(JSError) as excinfo:
                ctx.eval("throw new RangeError('out of bounds')")
            assert excinfo.value.name == "RangeError"
            assert excinfo.value.message == "out of bounds"
            assert excinfo.value.stack is not None


def test_host_error_cause_points_at_original_python_exception() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            @ctx.function
            def fail() -> None:
                raise KeyError("missing")

            with pytest.raises(HostError) as excinfo:
                ctx.eval("fail()")
            assert isinstance(excinfo.value.__cause__, KeyError)


def test_host_error_cause_is_cleared_between_evals() -> None:
    """A successful host-fn call after a failing one must not inherit the
    previous __cause__. If the bridge's side-channel isn't cleared on
    consumption, a subsequent unrelated JSError could surface with a
    bogus Python traceback attached.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            @ctx.function
            def fail() -> None:
                raise KeyError("first call")

            # First eval raises HostError with a cause.
            with pytest.raises(HostError) as first:
                ctx.eval("fail()")
            assert isinstance(first.value.__cause__, KeyError)

            # A plain JS-thrown error that isn't HostError-named should
            # raise as JSError with no Python-side cause.
            with pytest.raises(JSError) as second:
                ctx.eval("throw new TypeError('unrelated')")
            assert second.value.name == "TypeError"
            assert second.value.__cause__ is None


def test_swallowed_host_raise_does_not_leak_cause_into_later_eval() -> None:
    """Host A raises; JS catches and drops the error; a LATER eval
    synthesizes a HostError-named throw by hand. That synthetic error
    must not inherit Host A's Python traceback — the side-channel needs
    to be cleared when a fresh user-facing eval starts.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            @ctx.function
            def swallowed() -> None:
                raise RuntimeError("swallowed in JS")

            # First eval catches the host error in JS and returns normally.
            # The raise still lands in the bridge's side-channel.
            assert ctx.eval(
                "try { swallowed(); 'unreachable'; } catch (e) { 'caught' }"
            ) == "caught"

            # Second eval synthesizes a HostError-named throw from pure JS
            # (no host function involved). Its __cause__ should be None —
            # the bridge must have cleared the channel at this eval's start.
            with pytest.raises(HostError) as excinfo:
                ctx.eval(
                    "const e = new Error('fake'); e.name = 'HostError'; throw e;"
                )
            assert excinfo.value.__cause__ is None


def test_js_catches_hosterror_and_reads_name_and_message() -> None:
    """The HostError the host throws must be catchable from JS with its
    name and message intact. ."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            @ctx.function
            def raiser() -> None:
                raise ValueError("inner detail")

            assert (
                ctx.eval(
                    "try { raiser(); 'unreachable'; }"
                    " catch (e) { `${e.name}: ${e.message}` }"
                )
                == "HostError: Host function failed"
            )


def test_non_error_throw_coerces_to_jserror() -> None:
    """`throw 'x'` / `throw 42` surface as JSError(name='Error',
    message=<coerced string>, stack=None). The shim coerces via ToString."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises(JSError) as s_exc:
                ctx.eval("throw 'bare string'")
            assert s_exc.value.name == "Error"
            assert s_exc.value.message == "bare string"
            assert s_exc.value.stack is None

            with pytest.raises(JSError) as n_exc:
                ctx.eval("throw 42")
            assert n_exc.value.name == "Error"
            assert n_exc.value.message == "42"
            assert n_exc.value.stack is None
