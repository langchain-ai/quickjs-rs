"""Shim-level verification for v0.2 promise registration.

§17.2 step 2 sign-off: qjs_register_host_function(is_async=1) creates a
JS value that:
  - has typeof === "function"
  - when called, returns a JS Promise

The bridge doesn't yet know how to dispatch host_call_async (that's
step 3). To verify the shim wiring without dragging in the full Context
stack, we poke the Bridge directly: register an async function via the
bridge, eval JS that calls it, then inspect the returned slot via the
shim's type_of / is_promise / promise_state exports.

The bridge's step-2 stub returns -1 from host_call_async (synchronous
rejection). That means the Promise returned to JS is already rejected
by the time control returns to eval. We verify the Promise is a Promise,
its state is "rejected", and the rejection reason is the marker HostError
the shim injects on the sync-rejection path.
"""

from __future__ import annotations

from quickjs_wasm import _msgpack
from quickjs_wasm._bridge import Bridge


def test_async_registration_produces_function_returning_promise() -> None:
    b = Bridge()
    rt = b.runtime_new()
    ctx = b.context_new(rt)

    # Register a nominal async host function. The callable itself is
    # irrelevant — the stub dispatch in the bridge never calls it —
    # but the bridge bookkeeping expects it present so register_host_function
    # doesn't balk.
    async def never_called() -> None:
        raise AssertionError("bridge should not dispatch in step 2")

    b.register_host_function(ctx, "foo", never_called, is_async=True)

    # Resolve globalThis.foo and check it's a function.
    glb_status, glb_slot = b.get_prop(
        ctx, b.get_global_object(ctx), "foo"
    )
    assert glb_status == 0 and glb_slot != 0
    # type_of returns KIND_FUNCTION (8) for a function.
    kind = b.type_of(ctx, glb_slot)
    assert kind == 8, f"expected KIND_FUNCTION(8), got {kind}"

    # Call the function with no args: eval returns the Promise.
    status, slot = b.eval(ctx, "foo()")
    assert status == 0, f"unexpected eval status {status}"

    # Promise identity + state.
    assert b.is_promise(ctx, slot), "foo() did not return a Promise"
    # §6.2: 0 = pending, 1 = fulfilled, 2 = rejected.
    state = b.promise_state(ctx, slot)
    assert state == 2, f"expected rejected(2), got state {state}"

    # Extract the rejection reason and verify it's the marker HostError
    # the shim builds on the sync-rejection path.
    rs_status, reason_slot = b.promise_result(ctx, slot)
    assert rs_status == 0 and reason_slot != 0
    mp_status, payload = b.to_msgpack(ctx, reason_slot)
    # The reason is a JS Error object — msgpack's object branch walks
    # own-enumerable properties, which for a bare Error with .name and
    # .message set to strings gives us a dict.
    assert mp_status == 0, f"to_msgpack on rejection reason failed: {mp_status}"
    reason = _msgpack.decode(payload)
    assert isinstance(reason, dict), f"reason is {type(reason).__name__}"
    assert reason.get("name") == "HostError"
    assert "host rejected" in reason.get("message", "")

    # Tidy up.
    b.slot_drop(ctx, reason_slot)
    b.slot_drop(ctx, slot)
    b.slot_drop(ctx, glb_slot)
    b.context_free(ctx)
    b.runtime_free(rt)
    b.close()


def test_sync_registration_still_works_after_is_async_param_added() -> None:
    """Regression check: v0.1 sync registration continues to work with
    the new is_async=False default. Guards against accidentally making
    the default async, or against mismatched trampoline dispatch."""
    b = Bridge()
    rt = b.runtime_new()
    ctx = b.context_new(rt)

    def doubled(x: int) -> int:
        return x * 2

    b.register_host_function(ctx, "dbl", doubled, is_async=False)
    status, slot = b.eval(ctx, "dbl(21)")
    assert status == 0
    mp_status, payload = b.to_msgpack(ctx, slot)
    assert mp_status == 0
    assert _msgpack.decode(payload) == 42.0

    b.slot_drop(ctx, slot)
    b.context_free(ctx)
    b.runtime_free(rt)
    b.close()
