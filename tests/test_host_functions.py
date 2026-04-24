"""Host function registration. See README.md"""

from __future__ import annotations

from quickjs_rs import HostError, Runtime


def test_reentrant_eval_from_host_function() -> None:
    """A host function that synchronously calls ctx.eval must see its own
    args survive intact. The shim's per-context scratch would otherwise be
    clobbered by the inner eval's to_msgpack; the bridge copies args out of
    guest memory at dispatch entry to sidestep that.

    This test runs a host function whose body re-evaluates JS that forces
    the scratch to grow past its current capacity (so a non-copying design
    would see realloc invalidate the still-pending args pointer).
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            call_count = 0

            @ctx.function
            def reentrant(value: int, payload_size: int) -> int:
                nonlocal call_count
                call_count += 1
                # Force the scratch to grow inside the inner eval. A 200 KB
                # payload was confirmed to trip the realloc path in
                # test_large_payload_overflow.
                big = ctx.eval(f"new Uint8Array({payload_size}).fill(1)")
                assert len(big) == payload_size
                return value * 2

            result = ctx.eval("reentrant(21, 200000)")
            assert result == 42
            assert call_count == 1


def test_host_function_args_are_copied_before_dispatch() -> None:
    """Separate, narrower assertion: the host_call dispatch reads its args
    from guest memory exactly once, before any user code runs. Any inner
    ctx.eval the user makes starts with a clean slate w.r.t. the shim's
    scratch. A regression here would manifest as garbled args on the
    second invocation of a host fn that re-enters.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            seen: list[str] = []

            @ctx.function
            def record(tag: str) -> str:
                # Re-entrant eval in between records to churn the scratch
                # with a different shape each time.
                ctx.eval("new Uint8Array(100000).fill(7)")
                seen.append(tag)
                return tag.upper()

            assert ctx.eval("record('alpha')") == "ALPHA"
            assert ctx.eval("record('beta')") == "BETA"
            assert seen == ["alpha", "beta"]


def test_host_function_exception_surfaces_as_hosterror() -> None:
    """Python exception out of a registered host function round-trips as
    HostError when it escapes back through ctx.eval. ."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            def explode() -> None:
                raise RuntimeError("bang")

            try:
                ctx.eval("explode()")
            except HostError as exc:
                assert "bang" in exc.message
            else:
                raise AssertionError("expected HostError")


def test_register_preserves_callable_identity() -> None:
    """@ctx.function is a decorator: it must return the original callable
    so patterns like `@ctx.function` above a docstring'd function don't
    erase the Python reference."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            def named_fn(x: int) -> int:
                return x + 1

            assert callable(named_fn)
            assert named_fn(5) == 6  # Still callable from Python directly.
            assert ctx.eval("named_fn(5)") == 6  # And from JS.


def test_register_with_name_override() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function(name="jsName")
            def python_name(x: int) -> int:
                return x * 10

            assert ctx.eval("jsName(4)") == 40
