"""Globals proxy. See spec/implementation.md §7.2, §7.3, §11.1."""

from __future__ import annotations

import pytest

from quickjs_wasm import Context, MarshalError, Runtime


def test_set_and_read_primitive(ctx: Context) -> None:
    ctx.globals["x"] = 42
    assert ctx.globals["x"] == 42
    assert ctx.eval("x") == 42


def test_set_and_read_string_with_unicode(ctx: Context) -> None:
    ctx.globals["greeting"] = "héllo 🌍"
    assert ctx.globals["greeting"] == "héllo 🌍"
    assert ctx.eval("greeting") == "héllo 🌍"


def test_set_and_read_object(ctx: Context) -> None:
    ctx.globals["data"] = {"n": 100, "items": [1, 2, 3]}
    assert ctx.globals["data"] == {"n": 100, "items": [1, 2, 3]}
    # JS side sees the same structure with expected nested access.
    assert ctx.eval("data.items[2]") == 3


def test_set_and_read_array(ctx: Context) -> None:
    ctx.globals["seq"] = [10, 20, 30]
    assert ctx.globals["seq"] == [10, 20, 30]
    assert ctx.eval("seq.length") == 3


def test_set_and_read_bytes(ctx: Context) -> None:
    """Python bytes → JS Uint8Array per §8's Python-side table."""
    ctx.globals["buf"] = b"\x01\x02\x03"
    assert ctx.globals["buf"] == b"\x01\x02\x03"
    assert ctx.eval("buf instanceof Uint8Array") is True
    assert ctx.eval("buf.length") == 3


def test_contains_returns_true_for_set_global(ctx: Context) -> None:
    ctx.globals["exists"] = 1
    assert "exists" in ctx.globals


def test_contains_returns_false_for_missing(ctx: Context) -> None:
    assert "definitely_not_set" not in ctx.globals


def test_contains_false_when_value_is_undefined(ctx: Context) -> None:
    """§7.3-adjacent semantics: ``in`` collapses the JS distinction
    between "own property set to undefined" and "not defined" — both
    are "not present" from Python's dict-like perspective. Documented
    in Globals docstring."""
    ctx.eval("globalThis.maybe = undefined")
    assert "maybe" not in ctx.globals


def test_reassignment_updates_value(ctx: Context) -> None:
    ctx.globals["counter"] = 1
    assert ctx.globals["counter"] == 1
    ctx.globals["counter"] = "two"
    assert ctx.globals["counter"] == "two"
    assert ctx.eval("typeof counter") == "string"


def test_unicode_key_names(ctx: Context) -> None:
    """JS property names are Unicode strings; the globals proxy must
    round-trip UTF-8 bytes for the key itself, not just the value."""
    ctx.globals["日本語"] = "ok"
    assert ctx.globals["日本語"] == "ok"
    assert ctx.eval("globalThis['日本語']") == "ok"


def test_shadowing_js_builtin(ctx: Context) -> None:
    """Setting a global that collides with a JS builtin actually
    clobbers the binding — there's no implicit protection. This is the
    same behavior as plain JS (`Math = 'clobber'` in a script). Users
    who care about sandboxing builtins from host-injected globals need
    to choose names that don't collide."""
    assert ctx.eval("typeof Math.PI") == "number"
    ctx.globals["Math"] = "clobber"
    assert ctx.globals["Math"] == "clobber"
    assert ctx.eval("Math") == "clobber"


def test_del_unsupported_raises_typeerror(ctx: Context) -> None:
    """§7.2 Globals signature defines __getitem__ / __setitem__ /
    __contains__ / get_handle — but not __delitem__. Python's default
    ``del`` on a dict-like without that method raises TypeError;
    locking the behavior in so we don't silently acquire a
    __delitem__ later without a spec update."""
    ctx.globals["throwaway"] = 1
    with pytest.raises((TypeError, AttributeError)):
        del ctx.globals["throwaway"]


def test_get_handle_not_yet_implemented(ctx: Context) -> None:
    """§7.2 declares Globals.get_handle for handle-valued reads; the
    implementation is wired to NotImplementedError until the handle
    integration with Globals lands. This test documents the status
    explicitly — if get_handle is ever wired, this test flips to
    assert real behavior rather than silently lying."""
    ctx.globals["x"] = 1
    with pytest.raises(NotImplementedError):
        ctx.globals.get_handle("x")


def test_handle_valued_assignment_not_yet_implemented(ctx: Context) -> None:
    """§7.2 Globals.__setitem__ accepts Handle | Any; the Handle
    branch lands with broader handle-integration work. Until then
    it raises NotImplementedError (not MarshalError — the Handle is
    well-formed; the wiring just hasn't landed)."""
    with ctx.eval_handle("({value: 42})") as h:
        with pytest.raises(NotImplementedError):
            ctx.globals["stored"] = h


def test_unsupported_python_type_raises_marshalerror(ctx: Context) -> None:
    """Assigning a Python value that isn't in §8's table (set here)
    surfaces as MarshalError, not a bare TypeError. §10.3 invariant:
    every public method either returns or raises a QuickJSError
    subclass."""
    with pytest.raises(MarshalError):
        ctx.globals["s"] = {1, 2, 3}


def test_globals_do_not_leak_across_contexts() -> None:
    """§13 / §7.3: two contexts on the same Runtime have independent
    global objects. Previously covered inline in the smoke test;
    lives here as its focused home since this is the canonical
    "globals" behavior."""
    with Runtime() as rt:
        with rt.new_context() as ctx_a, rt.new_context() as ctx_b:
            ctx_a.globals["alpha"] = "from_a"
            ctx_b.globals["beta"] = "from_b"
            assert ctx_a.eval("typeof beta") == "undefined"
            assert ctx_b.eval("typeof alpha") == "undefined"
            assert ctx_a.globals["alpha"] == "from_a"
            assert ctx_b.globals["beta"] == "from_b"


def test_missing_key_raises_via_getitem(ctx: Context) -> None:
    """Reading a missing global. In JS, `globalThis.missing` evaluates
    to undefined — not an error. Our ``__getitem__`` follows the same
    rule (coerces to Python None under default preserve_undefined=False)
    rather than raising KeyError. Lock it in — the dict-like KeyError
    expectation would be surprising given the JS semantics."""
    # No assignment performed — key genuinely absent.
    assert ctx.globals["never_set"] is None
