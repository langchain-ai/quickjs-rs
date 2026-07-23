"""Empty strings marshal across the host and guest boundary."""

from __future__ import annotations

from typing import Any

from quickjs_rs import Runtime


def _payload() -> dict[str, Any]:
    return {"state": "", "items": [{"state": ""}]}


def test_sync_host_return_can_contain_empty_strings() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            def empty() -> str:
                return ""

            ctx.register("empty", empty, is_async=False)
            ctx.register("payload", _payload, is_async=False)

            assert ctx.eval("empty()", module=False) == ""
            assert ctx.eval("payload()", module=False) == _payload()


async def test_async_host_return_can_contain_empty_strings() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def empty() -> str:
                return ""

            async def payload() -> dict[str, Any]:
                return _payload()

            ctx.register("empty", empty, is_async=True)
            ctx.register("payload", payload, is_async=True)

            assert await ctx.eval_async("await empty()", module=False) == ""
            assert await ctx.eval_async("await payload()", module=False) == _payload()
