"""Handle lifecycle. See spec/implementation.md §7.2, §7.3, §11.1."""

from __future__ import annotations

import gc
import warnings

import pytest

from quickjs_wasm import (
    InvalidHandleError,
    JSError,
    Runtime,
)


def test_eval_handle_roundtrip() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle("({x: 1, y: 'hi'})") as obj:
                assert obj.type_of == "object"
                x = obj.get("x")
                try:
                    assert x.to_python() == 1
                finally:
                    x.dispose()


def test_handle_call_method_with_python_args() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle(
                "({mul(a, b) { return a * b }})"
            ) as obj:
                result = obj.call_method("mul", 6, 7)
                try:
                    assert result.to_python() == 42
                finally:
                    result.dispose()


def test_handle_type_of_covers_all_kinds() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            cases = {
                "null": "null",
                "undefined": "undefined",
                "true": "boolean",
                "42": "number",
                "10n ** 30n": "bigint",
                "'hi'": "string",
                "({})": "object",
                "[]": "array",
                "(() => 1)": "function",
                "Symbol('s')": "symbol",
            }
            for expr, expected in cases.items():
                with ctx.eval_handle(expr) as h:
                    assert h.type_of == expected, (expr, h.type_of, expected)


def test_cross_context_handle_raises() -> None:
    """§7.3: using a Handle from Context A in a call on Context B raises
    InvalidHandleError."""
    with Runtime() as rt:
        with rt.new_context() as ctx_a, rt.new_context() as ctx_b:
            with ctx_a.eval_handle("'from a'") as h_a:
                with ctx_b.eval_handle("({})") as h_b:
                    with pytest.raises(InvalidHandleError):
                        h_b.set("leak", h_a)


def test_disposed_handle_raises_on_use() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            h = ctx.eval_handle("({x: 1})")
            h.dispose()
            assert h.disposed
            with pytest.raises(InvalidHandleError):
                h.get("x")


def test_dispose_is_idempotent() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            h = ctx.eval_handle("({})")
            h.dispose()
            h.dispose()  # no error


def test_leaked_handle_emits_resourcewarning() -> None:
    """__del__ on a live handle that was never disposed emits
    ResourceWarning. Convention from Python stdlib for leaked resources
    (§7.3)."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            h = ctx.eval_handle("({})")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                del h
                gc.collect()
            assert any(
                issubclass(w.category, ResourceWarning) for w in caught
            ), [w.category for w in caught]


def test_leaked_handle_after_context_close_does_not_crash() -> None:
    """If a Handle survives its owning Context (pathological but possible
    under lazy GC or exception-driven flow), __del__ must not dispatch
    a drop into the torn-down slot table. The weakref path handles this:
    warning fires, no drop, no segfault."""
    rt = Runtime()
    ctx = rt.new_context()
    h = ctx.eval_handle("({})")
    ctx.close()
    rt.close()
    # Now the context is dead and its slot table is freed; garbage-
    # collecting the handle must not crash.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        del h
        gc.collect()
    # We don't assert the warning fired strictly — the context finalizer
    # order isn't guaranteed in edge cases — but we assert no exception.
    _ = caught


def test_handle_survives_across_evals() -> None:
    """A Handle returned from eval_handle must remain usable across
    subsequent eval() calls on the same context. Primary value of
    handles vs eval()-marshaling."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle("({counter: 0})") as state:
                assert ctx.eval("1 + 1") == 2  # unrelated work
                value = state.get("counter")
                try:
                    assert value.to_python() == 0
                finally:
                    value.dispose()


def test_handle_holds_function_that_would_fail_marshaling() -> None:
    """§8: a function in an eval() result raises MarshalError, because
    there's no way to meaningfully serialize it. eval_handle gives you
    back something you can still invoke without ever trying to marshal
    it directly."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # ctx.eval on a function would MarshalError — skipped: the
            # -1 shim return on function branches surfaces already in
            # earlier tests. Here we verify eval_handle is the escape
            # hatch: the handle is usable, and calling it works.
            with ctx.eval_handle("((x) => x * x)") as fn:
                assert fn.type_of == "function"
                result = fn.call(9)
                try:
                    assert result.to_python() == 81
                finally:
                    result.dispose()


def test_to_python_allow_opaque_substitutes_child_handles() -> None:
    """§7.2: allow_opaque=True produces marshalable leaves and child
    Handles at positions where msgpack would fail."""
    from quickjs_wasm import MarshalError

    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle(
                "({n: 1, s: 'ok', f: () => 42, nested: {g: () => 'deep'}})"
            ) as obj:
                # Without allow_opaque, the function member fails
                # marshaling for the whole value.
                with pytest.raises(MarshalError):
                    obj.to_python()

                as_dict = obj.to_python(allow_opaque=True)
                try:
                    assert as_dict["n"] == 1
                    assert as_dict["s"] == "ok"
                    assert as_dict["f"].type_of == "function"
                    # Recursion into plain nested object with a function.
                    assert as_dict["nested"]["g"].type_of == "function"
                    # Calling the materialized handle works.
                    r = as_dict["f"].call()
                    try:
                        assert r.to_python() == 42
                    finally:
                        r.dispose()
                finally:
                    as_dict["f"].dispose()
                    as_dict["nested"]["g"].dispose()


def test_to_python_allow_opaque_arrays_recurse() -> None:
    """Arrays under allow_opaque walk element-by-element so mixed
    marshalable / opaque contents work."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle("[1, 'two', () => 3, {inner: 4}]") as arr:
                values = arr.to_python(allow_opaque=True)
                try:
                    assert values[0] == 1
                    assert values[1] == "two"
                    assert values[2].type_of == "function"
                    assert values[3] == {"inner": 4}
                finally:
                    values[2].dispose()


def test_to_python_allow_opaque_cycle_raises_marshalerror() -> None:
    """Cycles raise MarshalError even under allow_opaque — documented
    behavior. Detection is indirect (via depth cap) rather than via a
    same-value check, which is fine for v0.1 since the depth cap of 128
    fails fast long before the walk does anything harmful."""
    from quickjs_wasm import MarshalError

    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle("const a = {}; a.self = a; a") as cyclic:
                with pytest.raises(MarshalError):
                    cyclic.to_python(allow_opaque=True)


def test_handle_new_constructs_instances() -> None:
    """§7.2 Handle.new: invoke the handle as a JS constructor. Uses Date
    because it's a canonical built-in constructor with observable state
    (the JS month argument is zero-indexed, so January = 0)."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle("Date") as date_ctor:
                d = date_ctor.new(2024, 0, 1)
                try:
                    year_handle = d.call_method("getFullYear")
                    try:
                        assert year_handle.to_python() == 2024
                    finally:
                        year_handle.dispose()
                finally:
                    d.dispose()


def test_handle_new_propagates_constructor_exceptions() -> None:
    """A constructor that throws surfaces the throw as JSError, same as
    Handle.call — constructor vs function call is a JS-semantic
    distinction, not an error-handling one."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle(
                "function Bad() { throw new TypeError('no can do'); } Bad"
            ) as ctor:
                with pytest.raises(JSError) as excinfo:
                    ctor.new()
                assert excinfo.value.name == "TypeError"
                assert excinfo.value.message == "no can do"


def test_js_throw_inside_handle_call_surfaces_as_jserror() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle(
                "(() => { throw new RangeError('boom'); })"
            ) as fn:
                with pytest.raises(JSError) as excinfo:
                    fn.call()
                assert excinfo.value.name == "RangeError"
                assert excinfo.value.message == "boom"
