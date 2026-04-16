"""v0.1 acceptance test. See spec/implementation.md §13.

This is the north star. Each assertion represents functionality that must
work for v0.1 to ship. Assertions are un-skipped one commit at a time per
CLAUDE.md's commit discipline — every passing assertion is a natural
commit boundary.
"""

from __future__ import annotations

import pytest

from quickjs_wasm import (
    HostError,
    InvalidHandleError,  # noqa: F401 — exercised once handles land
    JSError,
    MarshalError,  # noqa: F401 — exercised when handle marshaling lands
    MemoryLimitError,
    Runtime,
    TimeoutError,
)


def test_smoke_primitives() -> None:
    """Greens the primitive block of §13 plus bigint and Uint8Array."""
    with Runtime(memory_limit=64 * 1024 * 1024) as rt:
        with rt.new_context(timeout=5.0) as ctx:
            assert ctx.eval("1 + 2") == 3
            assert ctx.eval("'hello'") == "hello"
            assert ctx.eval("true") is True
            assert ctx.eval("null") is None
            assert ctx.eval("undefined") is None
            assert ctx.eval("1.5") == 1.5

            # BigInt — positive and negative to exercise sign handling.
            assert ctx.eval("10n ** 30n") == 10**30
            assert ctx.eval("-(10n ** 30n)") == -(10**30)

            # Bytes
            assert ctx.eval("new Uint8Array([1, 2, 3])") == b"\x01\x02\x03"

            # Arrays (including nested to exercise recursion)
            assert ctx.eval("[1, 2, 3]") == [1, 2, 3]
            assert ctx.eval("[[1, 2], [3, 4]]") == [[1, 2], [3, 4]]
            assert ctx.eval("[]") == []

            # Objects (mixed nesting, empty, insertion order preserved)
            assert ctx.eval("({a: 1, b: [2, 3]})") == {"a": 1, "b": [2, 3]}
            assert ctx.eval("({})") == {}
            ordered = ctx.eval("({z: 1, a: 2, m: 3})")
            assert list(ordered.keys()) == ["z", "a", "m"]

            # Globals: read, write, contains
            ctx.globals["x"] = 42
            assert ctx.eval("x") == 42
            ctx.globals["data"] = {"n": 100}
            assert ctx.eval("data.n") == 100
            assert "x" in ctx.globals
            assert "not_a_real_global" not in ctx.globals
            assert ctx.globals["x"] == 42

            # Four-layer nested round-trip: encode → decode (from_msgpack) →
            # encode (to_msgpack) → decode. Exercises every container type.
            ctx.globals["deep"] = {"nested": {"list": [1, 2, {"leaf": "value"}]}}
            assert ctx.eval("deep.nested.list[2].leaf") == "value"

            # Host functions: decorator form
            @ctx.function
            def add(a: int, b: int) -> int:
                return a + b

            assert ctx.eval("add(1, 2)") == 3

            # Host functions: explicit form with a name override
            ctx.register("say_hi", lambda who: f"hi {who}")
            assert ctx.eval("say_hi('world')") == "hi world"

            # Host exception propagates out as HostError with __cause__
            # threaded back to the original Python exception.
            @ctx.function
            def boom() -> None:
                raise ValueError("from python")

            with pytest.raises(HostError) as excinfo:
                ctx.eval("boom()")
            assert isinstance(excinfo.value.__cause__, ValueError)

            # JS-side visibility: a try/catch inside JS sees the error's
            # name and message and can round-trip them back as a string.
            assert (
                ctx.eval(
                    "try { boom(); 'unreachable'; }"
                    " catch (e) { e.name + ': ' + e.message }"
                )
                == "HostError: from python"
            )

            # JS-thrown TypeError (not a host error) surfaces as JSError
            # with name=TypeError, message, and a populated stack.
            with pytest.raises(JSError) as js_excinfo:
                ctx.eval("throw new TypeError('bad thing')")
            assert js_excinfo.value.name == "TypeError"
            assert js_excinfo.value.message == "bad thing"
            assert js_excinfo.value.stack is not None

            # Memory limit: unbounded allocation trips JS_ATOM_out_of_memory,
            # surfaced as MemoryLimitError.
            with pytest.raises(MemoryLimitError):
                ctx.eval(
                    "let a = []; while(true) a.push(new Array(1e6).fill(0))"
                )

            # Timeout: infinite loop terminates within the configured
            # deadline. Context uses its default 5 s budget; this test
            # drops it to something short so pytest doesn't sit for 5 s
            # of wall time on every run.
            ctx.timeout = 0.2
            with pytest.raises(TimeoutError):
                ctx.eval("while(true){}")
            ctx.timeout = 5.0

            # Handles: eval_handle returns a Handle that outlives
            # its creating eval call, supports property access, method
            # invocation, and to_python marshaling for the subset of
            # values that are marshalable.
            with ctx.eval_handle(
                "({x: 1, y: 2, add(a, b) { return a + b }})"
            ) as obj:
                assert obj.type_of == "object"
                assert obj.get("x").to_python() == 1
                result = obj.call_method("add", 10, 20)
                assert result.to_python() == 30
                result.dispose()

            # Multi-context isolation: globals don't leak across
            # contexts sharing the same Runtime.
            with rt.new_context() as ctx2:
                ctx2.globals["y"] = "other"
                assert ctx2.eval("y") == "other"
                assert ctx.eval("typeof y") == "undefined"


@pytest.mark.skip(reason="Pending the rest of §7.2; greens assertion-by-assertion.")
def test_acceptance() -> None:
    with Runtime(memory_limit=64 * 1024 * 1024) as rt:
        with rt.new_context(timeout=5.0) as ctx:
            # Primitives
            assert ctx.eval("1 + 2") == 3
            assert ctx.eval("'hello'") == "hello"
            assert ctx.eval("true") is True
            assert ctx.eval("null") is None
            assert ctx.eval("undefined") is None
            assert ctx.eval("1.5") == 1.5

            # BigInt
            assert ctx.eval("10n ** 30n") == 10**30

            # Collections
            assert ctx.eval("[1, 2, 3]") == [1, 2, 3]
            assert ctx.eval("({a: 1, b: [2, 3]})") == {"a": 1, "b": [2, 3]}

            # Bytes
            result = ctx.eval("new Uint8Array([1, 2, 3])")
            assert result == b"\x01\x02\x03"

            # Globals (read/write)
            ctx.globals["x"] = 42
            assert ctx.eval("x") == 42
            ctx.globals["data"] = {"n": 100}
            assert ctx.eval("data.n") == 100

            # Host functions: decorator form
            @ctx.function
            def add(a: int, b: int) -> int:
                return a + b

            assert ctx.eval("add(1, 2)") == 3

            # Host functions: explicit form with name override
            ctx.register("say_hi", lambda name: f"hi {name}")
            assert ctx.eval("say_hi('world')") == "hi world"

            # JS exception → Python
            with pytest.raises(JSError) as excinfo:
                ctx.eval("throw new TypeError('bad thing')")
            assert excinfo.value.name == "TypeError"
            assert excinfo.value.message == "bad thing"
            assert excinfo.value.stack is not None

            # Host exception → JS → Python
            @ctx.function
            def boom() -> None:
                raise ValueError("from python")

            with pytest.raises(HostError) as excinfo_h:
                ctx.eval("boom()")
            assert isinstance(excinfo_h.value.__cause__, ValueError)

            # JS catching host error
            assert (
                ctx.eval(
                    """
                try { boom(); 'unreachable'; }
                catch (e) { e.name + ': ' + e.message }
            """
                )
                == "HostError: from python"
            )

            # Memory limit
            with pytest.raises(MemoryLimitError):
                ctx.eval("let a = []; while(true) a.push(new Array(1e6).fill(0))")

            # Timeout
            with pytest.raises(TimeoutError):
                ctx.eval("while(true){}")

            # Handles
            with ctx.eval_handle("({x: 1, y: 2, add(a, b) { return a + b }})") as obj:
                assert obj.type_of == "object"
                assert obj.get("x").to_python() == 1
                result = obj.call_method("add", 10, 20)
                assert result.to_python() == 30
                result.dispose()

                as_dict = obj.to_python(allow_opaque=True)
                assert as_dict["x"] == 1
                assert hasattr(as_dict["add"], "call")
                as_dict["add"].dispose()

            # Multiple contexts, one runtime
            with rt.new_context() as ctx2:
                ctx2.globals["y"] = "other"
                assert ctx2.eval("y") == "other"
                assert ctx.eval("typeof y") == "undefined"
