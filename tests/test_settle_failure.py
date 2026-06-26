"""Async host-call settle failures surface as catchable errors, not deadlocks.

When a host call completes but its result cannot be delivered into the guest
(``resolve_pending`` raises — e.g. the value can't be marshaled), the driving
loop must NOT leave the promise pending and report a spurious ``DeadlockError``.
Instead it retries once, then rejects the promise, threading the original
exception out via the host-exception side channel so the real cause surfaces.

``DeadlockError`` is reserved for a genuinely un-resolvable promise (no resolver
and no host work in flight).
"""

from __future__ import annotations

import pytest

from quickjs_rs import DeadlockError, MarshalError, Runtime


def _nested(depth: int) -> dict:
    """A chain of ``depth`` nested dicts — exceeds the marshal recursion
    limit (``_MAX_MARSHAL_DEPTH`` == 128) for large ``depth``."""
    root: dict = {}
    cur = root
    for _ in range(depth):
        nxt: dict = {}
        cur["x"] = nxt
        cur = nxt
    return root


async def test_unmarshalable_result_rejects_not_deadlock() -> None:
    """A result too deeply nested to marshal back surfaces as a catchable
    MarshalError, not a DeadlockError."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def bad_result() -> dict:
                return _nested(200)

            ctx.register("badResult", bad_result, is_async=True)
            with pytest.raises(MarshalError):
                await ctx.eval_async("await badResult()", module=False)


async def test_nonmarshalable_leaf_rejects_not_deadlock() -> None:
    """A non-marshalable leaf type (a Python set) surfaces as MarshalError,
    not a DeadlockError, and names the real cause."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def set_result() -> set:
                return {1, 2, 3}

            ctx.register("setResult", set_result, is_async=True)
            with pytest.raises(MarshalError, match="cannot marshal"):
                await ctx.eval_async("await setResult()", module=False)


async def test_promise_all_with_one_unmarshalable_rejects() -> None:
    """Mirrors the agent fan-out: Promise.all over several host calls where one
    result can't be delivered. The batch rejects with the real error — it does
    not hang or deadlock, and the healthy siblings don't mask the failure."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def bad_result() -> dict:
                return _nested(200)

            async def good() -> dict:
                return {"ok": 1}

            ctx.register("badResult", bad_result, is_async=True)
            ctx.register("good", good, is_async=True)
            with pytest.raises(MarshalError):
                await ctx.eval_async(
                    "await Promise.all([badResult(), good()])", module=False
                )


async def test_original_cause_is_surfaced() -> None:
    """The exception the agent sees is the real marshaling failure, not the
    generic 'missing resolver' deadlock text — i.e. the cause is threaded out
    rather than swallowed."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def bad_result() -> dict:
                return _nested(200)

            ctx.register("badResult", bad_result, is_async=True)
            with pytest.raises(MarshalError) as excinfo:
                await ctx.eval_async("await badResult()", module=False)
            assert "recursion limit" in str(excinfo.value)
            assert not isinstance(excinfo.value, DeadlockError)


async def test_resolver_less_await_still_deadlocks() -> None:
    """Regression guard: the settle-failure recovery must NOT suppress a
    genuine dead-end. A promise with no resolver and no host work in flight
    still raises DeadlockError."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises(DeadlockError):
                await ctx.eval_async("await new Promise(() => {})", module=False)


async def test_healthy_async_result_unaffected() -> None:
    """Control: the fix does not touch the happy path — a normal async host
    result still resolves and marshals back."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def good() -> dict:
                return {"ok": 1}

            ctx.register("good", good, is_async=True)
            assert await ctx.eval_async("await good()", module=False) == {"ok": 1}
