"""Reentrant cross-context isolation tripwires.

"Reentrant context" here means an inner QuickJS operation is started
before an outer one has unwound, while both share the same runtime.
Typical shape: JS in context A calls a Python host function, and that
host function calls ``ctx_b.eval(...)`` (sync or async path). The key
invariant is context identity: the inner operation must execute against
the explicitly requested context (B), never whichever sibling context
was active at outer entry (A).
"""

from __future__ import annotations

from quickjs_rs import Runtime


def test_reentrant_eval_uses_requested_context_for_reads() -> None:
    """A host fn registered on context A that calls ``b.eval`` must run in
    B's global object, not A's."""
    with Runtime() as rt:
        with rt.new_context() as ctx_a, rt.new_context() as ctx_b:
            ctx_a.globals["tag"] = "A"
            ctx_b.globals["tag"] = "B"

            @ctx_a.function(name="call_b")
            def call_b() -> str:
                return ctx_b.eval("tag")

            assert ctx_a.eval("call_b()") == "B"


def test_reentrant_eval_uses_requested_context_for_writes() -> None:
    """Writes through ``b.eval`` from an A-registered host fn must mutate
    B, leaving A unchanged."""
    with Runtime() as rt:
        with rt.new_context() as ctx_a, rt.new_context() as ctx_b:
            ctx_a.globals["tag"] = "A"
            ctx_b.globals["tag"] = "B"

            @ctx_a.function(name="write_b")
            def write_b() -> None:
                ctx_b.eval("tag = 'B2'")

            ctx_a.eval("write_b()")
            assert ctx_a.eval("tag") == "A"
            assert ctx_b.eval("tag") == "B2"


async def test_async_host_fn_cross_context_eval_reads_requested_context() -> None:
    """Cross-context read isolation under async host callbacks."""
    with Runtime() as rt:
        with rt.new_context() as ctx_a, rt.new_context() as ctx_b:
            ctx_a.globals["tag"] = "A"
            ctx_b.globals["tag"] = "B"

            @ctx_a.function(name="call_b", is_async=True)
            async def call_b() -> str:
                return ctx_b.eval("tag")

            assert await ctx_a.eval_async("await call_b()") == "B"


async def test_async_host_fn_cross_context_eval_writes_requested_context() -> None:
    """Cross-context write isolation under async host callbacks."""
    with Runtime() as rt:
        with rt.new_context() as ctx_a, rt.new_context() as ctx_b:
            ctx_a.globals["tag"] = "A"
            ctx_b.globals["tag"] = "B"

            @ctx_a.function(name="write_b", is_async=True)
            async def write_b() -> None:
                ctx_b.eval("tag = 'B2'")

            await ctx_a.eval_async("await write_b()")
            assert ctx_a.eval("tag") == "A"
            assert ctx_b.eval("tag") == "B2"


async def test_async_host_fn_nested_reentrancy_preserves_context_identity() -> None:
    """Async path variant of the reentrant cross-context identity seam."""
    with Runtime() as rt:
        with rt.new_context() as ctx_a, rt.new_context() as ctx_b:
            ctx_a.globals["tag"] = "A"
            ctx_b.globals["tag"] = "B"

            @ctx_b.function(name="call_a")
            def call_a() -> str:
                return ctx_a.eval("tag")

            @ctx_a.function(name="via_b", is_async=True)
            async def via_b() -> str:
                # Reentrant chain inside async task:
                # ctx_a async host fn -> ctx_b.eval -> ctx_b host fn -> ctx_a.eval.
                return ctx_b.eval("call_a()")

            assert await ctx_a.eval_async("await via_b()") == "A"
