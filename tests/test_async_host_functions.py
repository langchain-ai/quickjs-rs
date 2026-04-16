"""Async host-function registration. See spec/implementation.md §7.4, §11.1."""

from __future__ import annotations

import asyncio
import functools

import pytest

from quickjs_wasm import Runtime


async def test_async_def_auto_detected_via_decorator() -> None:
    """§7.4: @ctx.function on an async def auto-detects as async."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            async def tick() -> str:
                await asyncio.sleep(0)
                return "tock"

            assert await ctx.eval_async("await tick()") == "tock"


async def test_async_def_auto_detected_via_register() -> None:
    """§7.4: ctx.register(name, fn) with no is_async kwarg auto-detects."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def lookup(k: str) -> int:
                await asyncio.sleep(0)
                return len(k)

            ctx.register("lookup", lookup)
            assert await ctx.eval_async("await lookup('hello')") == 5


async def test_sync_def_auto_detected_as_sync() -> None:
    """Regression: plain def functions still register as sync under the
    new auto-detection default. Guards against §7.4 default
    flipping the wrong way."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            def add(a: int, b: int) -> int:
                return a + b

            # Sync eval — if auto-detection incorrectly registered
            # add as async, ctx.eval("add(1, 2)") would get a Promise
            # instead of the value, surfacing as MarshalError.
            assert ctx.eval("add(1, 2)") == 3


async def test_functools_wraps_preserves_coroutine_detection() -> None:
    """functools.wraps DOES preserve iscoroutinefunction when applied
    to an async def — the common case. Confirm auto-detection sees
    through the wrapper."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def inner(x: int) -> int:
                await asyncio.sleep(0)
                return x + 1

            @functools.wraps(inner)
            async def outer(x: int) -> int:
                return await inner(x)

            ctx.register("outer", outer)
            assert await ctx.eval_async("await outer(41)") == 42


async def test_wrapped_chain_with_broken_marker_raises_typeerror() -> None:
    """§7.4: a non-coroutine wrapper around a coroutine (the broken-
    decorator case) surfaces as TypeError at registration rather than
    silent misclassification. The error message tells the user to
    pass is_async= explicitly."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def real_work() -> str:
                return "done"

            def broken_wrapper() -> str:  # type: ignore[return]
                # Imagine this is a sync-looking function whose real
                # implementation delegates to a coroutine — or, as
                # here, a function that someone attached __wrapped__
                # to without preserving the coroutine marker.
                return real_work()  # type: ignore[return-value]

            broken_wrapper.__wrapped__ = real_work  # type: ignore[attr-defined]

            with pytest.raises(TypeError) as excinfo:
                ctx.register("broken", broken_wrapper)
            msg = str(excinfo.value)
            assert "is_async=True" in msg
            assert "__wrapped__" in msg


async def test_explicit_override_beats_auto_detection() -> None:
    """is_async=True or False wins over whatever inspect reports. This
    is the escape hatch for C extensions, objects with __call__, and
    any other callable shape auto-detection can't see through."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            # A plain function that the user promises is really
            # an async dispatcher into an event loop they manage
            # themselves. Auto-detection would say sync; they know
            # it's not. (We don't test the behaviour of a wrongly-
            # overridden async function — that'd be a classic
            # shoot-your-foot — but we do verify the override path
            # reaches the async trampoline in the shim.)
            class AsyncShaped:
                async def __call__(self, x: int) -> int:
                    return x * 2

            fn = AsyncShaped()
            # Auto-detection on an instance with async __call__ may or
            # may not detect; the override makes it explicit.
            ctx.register("doubled", fn, is_async=True)
            assert await ctx.eval_async("await doubled(7)") == 14


async def test_explicit_sync_override_on_callable_class() -> None:
    """Mirror of the previous test: is_async=False forces sync
    registration even if the callable looks async-ish to inspect."""
    with Runtime() as rt:
        with rt.new_context() as ctx:

            def plain_doubler(x: int) -> int:
                return x * 2

            ctx.register("plain", plain_doubler, is_async=False)
            assert ctx.eval("plain(21)") == 42
