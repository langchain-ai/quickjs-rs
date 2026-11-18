"""Primitive round-trip. See README.md."""

from __future__ import annotations

from quickjs_rs import Runtime


def test_large_payload_overflow() -> None:
    """The shim's per-context msgpack scratch starts small and grows on demand.

    calls out this buffer as open for tuning — the current 64 KB initial
    fast path is smaller than the 1 MB default, so a 200 KB
    payload forces two reallocations. This test exercises that overflow path
    and confirms the (out_ptr, out_len) contract from /the Python
    side must re-read both every call (the buffer may have moved).
    """
    size = 200_000
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # Bytes: grows the scratch to ~200 KB.
            result = ctx.eval(f"new Uint8Array({size}).fill(42)")
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


def test_lone_surrogates_cross_both_ways() -> None:
    """JS strings may hold unpaired surrogates (e.g. from JSON.parse); they
    round-trip losslessly, matching json.loads on the host side."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.register("echo", lambda s: s, is_async=False)
            ctx.register("make", lambda: "a\ud800b", is_async=False)

            assert ctx.eval("JSON.parse('\"\\\\ud800\"')") == "\ud800"
            assert ctx.eval("['\\udc00x', {k: '\\ud83d'}]") == ["\udc00x", {"k": "\ud83d"}]
            assert ctx.eval("make().charCodeAt(1)") == 0xD800
            assert ctx.eval("echo('\\udfff') === '\\udfff'") is True
            assert ctx.eval("echo({v: '\\udfff'}).v.length") == 1

            ctx.globals["k\udc00"] = "v\ud800"
            assert ctx.globals["k\udc00"] == "v\ud800"
            assert ctx.eval("globalThis['k\\udc00'] === 'v\\ud800'") is True

            # Paired surrogates still decode as one code point.
            assert ctx.eval("'\\ud83d\\ude00'") == "\U0001f600"
