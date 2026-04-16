"""v0.1 acceptance test. See spec/implementation.md §13.

This is the north star. Each assertion represents functionality that must
work for v0.1 to ship. Work through it assertion-by-assertion — each passing
assertion is a natural commit boundary per CLAUDE.md.
"""

from __future__ import annotations

import pytest

from quickjs_wasm import (
    HostError,
    InvalidHandleError,  # noqa: F401 — exercised implicitly once handles land
    JSError,
    MarshalError,  # noqa: F401 — exercised when handle marshaling lands
    MemoryLimitError,
    Runtime,
    TimeoutError,
)


@pytest.mark.skip(reason="Pending Runtime/Context implementation (§7.2).")
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
