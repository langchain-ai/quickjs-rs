"""Whole-memory snapshot/restore

This plane uses a WHOLE-MEMORY snapshot: the entire guest linear memory +
__stack_pointer is captured as a flat image, so closures, pending promises,
full object graphs, and aliasing all survive — a strict superset of what the
old selective-value model could do. The old model's surface (registry tracking,
on_missing_name/on_unserializable policies, tombstones, dump_handle/load_handle)
is intentionally gone; these tests target the whole-memory contract.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from quickjs_rs import ConcurrentEvalError, QuickJSError, Runtime, Snapshot

# --- basic roundtrip + the capabilities the old model couldn't do -----------


def test_create_snapshot_roundtrip() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const test = 123;")
            data = ctx.create_snapshot().to_bytes()

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            snap = Snapshot.from_bytes(data)
            rt2.restore_snapshot(snap, ctx2)
            assert ctx2.eval("test") == 123


def test_snapshot_restore_preserves_aliasing() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const shared = { n: 1 }; const a = shared; const b = shared;")
            snap = ctx.create_snapshot()

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            rt2.restore_snapshot(snap, ctx2)
            assert ctx2.eval("a === b") is True
            # And the shared object is genuinely shared after restore:
            ctx2.eval("a.n = 99")
            assert ctx2.eval("b.n") == 99


def test_snapshot_preserves_closure_state() -> None:
    """A closure's captured variable survives — the headline whole-memory
    capability the selective-value model could not express."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval(
                """
                globalThis.makeCounter = () => { let n = 0; return () => ++n; };
                globalThis.inc = globalThis.makeCounter();
                globalThis.inc(); globalThis.inc();  // n is now 2
                """
            )
            snap = ctx.create_snapshot()

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            rt2.restore_snapshot(snap, ctx2)
            assert ctx2.eval("globalThis.inc()") == 3
            assert ctx2.eval("globalThis.inc()") == 4


def test_restore_snapshot_overwrites_existing_globals() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const keep = 7;")
            snap = ctx.create_snapshot()

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            ctx2.eval("globalThis.keep = 999;")
            assert ctx2.eval("keep") == 999
            rt2.restore_snapshot(snap, ctx2)
            assert ctx2.eval("keep") == 7


# --- fail-closed header validation (build identity + format version) --------


def test_restore_snapshot_unknown_format_version_rejected() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const x = 1;")
            data = bytearray(ctx.create_snapshot().to_bytes())

    # format_version is the u32 right after the 4-byte magic.
    data[4] = 2
    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            with pytest.raises(ValueError, match="format version"):
                rt2.restore_snapshot(Snapshot.from_bytes(bytes(data)), ctx2)


def test_restore_snapshot_build_id_mismatch_rejected() -> None:
    """A snapshot from a DIFFERENT guest build is rejected fail-closed — the
    build-identity guard quickjs-wasi lacks (catches version skew on rebuild)."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const x = 1;")
            data = bytearray(ctx.create_snapshot().to_bytes())

    # Corrupt a byte inside the build_id field (offset 8..40).
    data[10] ^= 0xFF
    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            with pytest.raises(ValueError, match="build_id"):
                rt2.restore_snapshot(Snapshot.from_bytes(bytes(data)), ctx2)
            # The instance was NOT mutated (fail before write).
            assert ctx2.eval("typeof x") == "undefined"


def test_restore_snapshot_bad_magic_rejected() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const x = 1;")
            data = bytearray(ctx.create_snapshot().to_bytes())
    data[0:4] = b"XXXX"
    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            with pytest.raises(ValueError, match="magic"):
                rt2.restore_snapshot(Snapshot.from_bytes(bytes(data)), ctx2)


def test_restore_snapshot_truncated_image_rejected() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const x = 1;")
            data = ctx.create_snapshot().to_bytes()
    truncated = data[: len(data) - 1000]  # image shorter than header memory_size
    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            with pytest.raises(ValueError, match="memory_size|length"):
                rt2.restore_snapshot(Snapshot.from_bytes(truncated), ctx2)


def test_restore_snapshot_no_inject_globals_validates_only() -> None:
    """inject_globals=False validates the header but does NOT write the image
    — the destination is left untouched."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const noInject = 77;")
            snap = ctx.create_snapshot()

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            rt2.restore_snapshot(snap, ctx2, inject_globals=False)
            assert (
                ctx2.eval("Object.prototype.hasOwnProperty.call(globalThis, 'noInject')") is False
            )


# --- async + eval_async-binding survival ------------------------------------


async def test_create_snapshot_async_roundtrip() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            await ctx.eval_async(
                "const value = 42; const shared = {}; const a = shared; const b = shared;"
            )
            snap = await ctx.create_snapshot_async()

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            rt2.restore_snapshot(snap, ctx2)
            assert ctx2.eval("value") == 42
            assert ctx2.eval("a === b") is True


@pytest.mark.parametrize(
    ("setup", "probe"),
    [
        ("const story = 'hi'", "story"),
        ("const story = await Promise.resolve('hi')", "story"),
        ("await Promise.resolve('x'); const story = 'hi'", "story"),
        ("let story", "story = await Promise.resolve('hi'); story"),
    ],
    ids=[
        "non-await-declaration",
        "top-level-await-declaration",
        "await-before-declaration",
        "predeclared-then-await-assign",
    ],
)
async def test_snapshot_roundtrip_preserves_eval_async_bindings(setup: str, probe: str) -> None:
    with Runtime(memory_limit=64 * 1024 * 1024) as runtime:
        with runtime.new_context(timeout=5.0) as ctx:
            await ctx.eval_async(setup, timeout=5.0)
            before = await ctx.eval_async(probe, timeout=5.0)
            payload = ctx.create_snapshot().to_bytes()
        with runtime.new_context(timeout=5.0) as ctx2:
            runtime.restore_snapshot(Snapshot.from_bytes(payload), ctx2)
            after = await ctx2.eval_async(probe, timeout=5.0)
    assert before == "hi"
    assert after == "hi"


# --- quiescence guards (snapshot at a coherent point only) ------------------


def test_create_snapshot_module_mode_guard_sync() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle("globalThis.modTouched = 1", module=True):
                pass
            with pytest.raises(NotImplementedError, match="module"):
                ctx.create_snapshot()


async def test_create_snapshot_module_mode_guard_async() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            await ctx.eval_async("globalThis.modTouched = 1", module=True)
            with pytest.raises(NotImplementedError, match="module"):
                ctx.create_snapshot()


async def test_create_snapshot_rejects_in_flight_eval_async() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            started = asyncio.Event()
            released = asyncio.Event()

            async def block() -> int:
                started.set()
                await released.wait()
                return 1

            ctx.register("block", block)
            eval_task = asyncio.create_task(ctx.eval_async("await block()"))
            await started.wait()
            with pytest.raises(ConcurrentEvalError, match="eval_async is in flight"):
                ctx.create_snapshot()
            with pytest.raises(ConcurrentEvalError, match="eval_async is in flight"):
                await ctx.create_snapshot_async()
            released.set()
            assert await eval_task == 1


async def test_create_snapshot_rejects_when_pending_host_tasks_exist() -> None:
    """Guard the quiescence check directly: a pending async host task means the
    job queue is mid-flight, so the captured heap would be incoherent."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            sleeper = asyncio.create_task(asyncio.sleep(60))
            ctx._pending_tasks[4242] = sleeper
            try:
                with pytest.raises(QuickJSError, match="async host tasks are pending"):
                    ctx.create_snapshot()
                with pytest.raises(QuickJSError, match="async host tasks are pending"):
                    await ctx.create_snapshot_async()
            finally:
                ctx._pending_tasks.pop(4242, None)
                sleeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sleeper


# --- runtime factory forms --------------------------------------------------


def test_runtime_create_snapshot_sync() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const viaRuntime = 10;")
            snap = rt.create_snapshot(ctx)
            assert isinstance(snap, Snapshot)


async def test_runtime_create_snapshot_async() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            await ctx.eval_async("const viaRuntimeAsync = 20;")
            snap = await rt.create_snapshot_async(ctx)
            assert isinstance(snap, Snapshot)
