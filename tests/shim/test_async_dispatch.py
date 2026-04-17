"""Shim-level verification for v0.2 async dispatch end-to-end.

§17.2 step 3 sign-off: with the real host_call_async dispatcher wired,
registering an async Python function and calling it from JS should:

  1. Create a Promise on the JS side (shim trampoline).
  2. Schedule an asyncio task on the running loop (bridge dispatcher).
  3. Run the coroutine to completion.
  4. Settle the Promise via qjs_promise_resolve (host task completion).
  5. Drive pending jobs so JS sees the fulfilled state.

The test plumbing here is deliberately ugly — eval_handle_async and
eval_async don't exist yet. Step 5's driving loop replaces the manual
pump with a clean await. This test will be retired (or rewritten
against the ergonomic surface) in step 5, but while it exists it
proves the dispatch pipeline works without depending on the driving
loop.

The test is marked async so pytest-asyncio (asyncio_mode=auto) gives
us a running loop — the bridge's dispatcher needs one to schedule
tasks against. In a plain sync test there's no loop, and the
dispatcher would return -1 (the step-2 synchronous-rejection path).
"""

from __future__ import annotations

import asyncio

from quickjs_rs import _msgpack
from quickjs_rs._bridge import Bridge


async def test_async_dispatch_fulfilled() -> None:
    b = Bridge()
    rt = b.runtime_new()
    ctx = b.context_new(rt)

    async def slow(x: int) -> int:
        await asyncio.sleep(0.001)
        return x * 2

    b.register_host_function(ctx, "slow", slow, is_async=True)

    # eval returns the pending Promise slot. dispatcher has scheduled
    # an asyncio task for `slow(21)` on the running loop.
    status, promise_slot = b.eval(ctx, "slow(21)")
    assert status == 0
    assert b.is_promise(ctx, promise_slot)
    # Pending immediately — the task hasn't had a chance to run yet.
    assert b.promise_state(ctx, promise_slot) == 0

    # Yield so the asyncio task runs. The task awaits sleep(0.001);
    # one sleep() cycle should be sufficient, but we wait on the
    # completion event the bridge maintains for step 5's driving loop.
    assert b._pending_completed is not None
    await b._pending_completed.wait()
    b._pending_completed.clear()

    # Task has settled the Promise via qjs_promise_resolve. The state
    # transition happens synchronously inside resolve; no pending-jobs
    # drain is needed to observe it.
    assert b.promise_state(ctx, promise_slot) == 1, "expected fulfilled"

    # Drain pending jobs as a smoke test that run_pending_jobs works
    # (step 5 needs this). Zero jobs run because no .then handlers
    # were attached, but the call itself should succeed with status 0.
    rc, count = b.runtime_run_pending_jobs(rt)
    assert rc == 0, f"run_pending_jobs failed: {rc}"
    assert count == 0

    # Extract and verify the fulfilled value.
    rs_status, result_slot = b.promise_result(ctx, promise_slot)
    assert rs_status == 0
    mp_status, payload = b.to_msgpack(ctx, result_slot)
    assert mp_status == 0
    assert _msgpack.decode(payload) == 42.0  # JS numbers are f64 per §8

    b.slot_drop(ctx, result_slot)
    b.slot_drop(ctx, promise_slot)
    b.context_free(ctx)
    b.runtime_free(rt)
    b.close()


async def test_async_dispatch_rejected_on_python_raise() -> None:
    """Python exception inside the async host fn → JS Promise rejects
    with a HostError record. §10.2 async path."""
    b = Bridge()
    rt = b.runtime_new()
    ctx = b.context_new(rt)

    async def failing() -> None:
        await asyncio.sleep(0)
        raise ValueError("boom from async")

    b.register_host_function(ctx, "failing", failing, is_async=True)

    status, promise_slot = b.eval(ctx, "failing()")
    assert status == 0
    assert b.is_promise(ctx, promise_slot)

    assert b._pending_completed is not None
    await b._pending_completed.wait()
    b._pending_completed.clear()

    assert b.promise_state(ctx, promise_slot) == 2, "expected rejected"

    rs_status, reason_slot = b.promise_result(ctx, promise_slot)
    assert rs_status == 0
    mp_status, payload = b.to_msgpack(ctx, reason_slot)
    assert mp_status == 0
    reason = _msgpack.decode(payload)
    assert isinstance(reason, dict)
    assert reason["name"] == "HostError"
    assert "boom from async" in reason["message"]

    b.slot_drop(ctx, reason_slot)
    b.slot_drop(ctx, promise_slot)
    b.context_free(ctx)
    b.runtime_free(rt)
    b.close()


async def test_run_pending_jobs_drains_then_reactions() -> None:
    """Once an async host call settles, any JS `.then` handler attached
    to the returned Promise runs as a microtask and must be drained
    via qjs_runtime_run_pending_jobs before its side effects are
    observable. Without the drain, the .then callback sits in the job
    queue forever."""
    b = Bridge()
    rt = b.runtime_new()
    ctx = b.context_new(rt)

    async def value() -> int:
        return 7

    b.register_host_function(ctx, "value", value, is_async=True)

    status, _ = b.eval(
        ctx,
        """
        globalThis._chain = value().then(v => { globalThis._seen = v * 10; });
        """,
    )
    assert status == 0

    # Wait for the host task to complete (resolves the inner Promise).
    assert b._pending_completed is not None
    await b._pending_completed.wait()
    b._pending_completed.clear()

    # Before drain: the .then handler hasn't run yet. _seen is undefined.
    status, seen_slot = b.eval(ctx, "typeof globalThis._seen")
    assert status == 0
    _, payload = b.to_msgpack(ctx, seen_slot)
    assert _msgpack.decode(payload) == "undefined"
    b.slot_drop(ctx, seen_slot)

    # Drain: the .then callback runs as a microtask.
    rc, count = b.runtime_run_pending_jobs(rt)
    assert rc == 0
    assert count >= 1  # at least the .then handler ran

    # Now _seen == 70.
    status, seen_slot = b.eval(ctx, "globalThis._seen")
    assert status == 0
    _, payload = b.to_msgpack(ctx, seen_slot)
    assert _msgpack.decode(payload) == 70.0
    b.slot_drop(ctx, seen_slot)

    b.context_free(ctx)
    b.runtime_free(rt)
    b.close()


