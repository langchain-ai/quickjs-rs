"""Primitive round-trip. See README.md."""

from __future__ import annotations

from quickjs_rs import Runtime


def test_large_payload_overflow() -> None:
    """The shim's per-context msgpack scratch starts small and grows on demand.

    calls out this buffer as open for tuning — the current 64 KB initial
    fast path is smaller than the 1 MB value the spec floats, so a 200 KB
    payload forces two reallocations. This test exercises that overflow path
    and confirms the (out_ptr, out_len) contract from /the Python
    side must re-read both every call (the buffer may have moved).
    """
    size = 200_000
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # Bytes: grows the scratch to ~200 KB.
            result = ctx.eval(
                f"new Uint8Array({size}).fill(42)"
            )
            assert isinstance(result, bytes)
            assert len(result) == size
            assert result[0] == 42 and result[-1] == 42

            # Subsequent call reuses the now-large scratch with a small value
            # — verifies the invalidation invariant: re-read (out_ptr, out_len)
            # each call rather than assuming the previous pointer is live.
            assert ctx.eval("1 + 2") == 3

            # Larger BigInt than any fast-path header assumption, just to put
            # a third distinct payload through the same buffer.
            big = 10**100
            assert ctx.eval(f"{big}n") == big
