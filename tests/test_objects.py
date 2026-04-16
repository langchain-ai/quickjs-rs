"""Object and array round-trip. See spec/implementation.md §8, §11.1."""

from __future__ import annotations

from quickjs_wasm import Context, Runtime


def test_empty_object_roundtrips(ctx: Context) -> None:
    assert ctx.eval("({})") == {}


def test_empty_array_roundtrips(ctx: Context) -> None:
    assert ctx.eval("[]") == []


def test_deeply_nested_objects(ctx: Context) -> None:
    """Ten levels of nesting is well within MARSHAL_MAX_DEPTH (128) but
    deep enough that a buggy recursion path would surface as a stack or
    decode error rather than silently producing the wrong shape."""
    expr = "".join(["({a:"] * 10) + "42" + ("})" * 10)
    result = ctx.eval(expr)
    cursor = result
    for _ in range(10):
        assert isinstance(cursor, dict) and set(cursor.keys()) == {"a"}
        cursor = cursor["a"]
    assert cursor == 42


def test_unicode_keys_roundtrip(ctx: Context) -> None:
    """§8: string keys are UTF-8 on the wire. Non-ASCII keys must survive
    both JS Atom → msgpack str and msgpack str → Python str."""
    result = ctx.eval("({'café': 1, '日本語': 2, '🔥': 'spicy'})")
    assert result == {"café": 1, "日本語": 2, "🔥": "spicy"}


def test_numeric_string_keys_vs_array(ctx: Context) -> None:
    """JS distinguishes `{0: 'a', 1: 'b'}` (an Object whose keys are the
    strings "0"/"1") from `['a', 'b']` (an Array with dense indices).
    Both should round-trip as distinct Python shapes."""
    as_obj = ctx.eval("({0: 'a', 1: 'b'})")
    assert as_obj == {"0": "a", "1": "b"}
    as_arr = ctx.eval("['a', 'b']")
    assert as_arr == ["a", "b"]
    assert type(as_obj) is dict
    assert type(as_arr) is list


def test_key_insertion_order_preserved(ctx: Context) -> None:
    """§8: plain Object is map with insertion-ordered str keys. Only
    applies to non-integer-indexed keys — JS itself enumerates
    integer-looking keys first in numeric order, then string keys in
    insertion order. This test uses only string keys so the insertion-
    order invariant is the only observable effect."""
    result = ctx.eval("({z: 1, m: 2, a: 3, beta: 4})")
    assert list(result.keys()) == ["z", "m", "a", "beta"]


def test_integer_keys_are_enumerated_before_string_keys(ctx: Context) -> None:
    """JS Object.keys / for...in emit integer-index keys first in numeric
    order, then string keys in insertion order — regardless of the
    source order in the object literal. Our marshaling inherits that
    ordering via JS_GetOwnPropertyNames. Lock it in so a future shim
    change to the enum flags doesn't silently alter iteration order."""
    result = ctx.eval("({z: 1, '2': 'two', a: 3, '0': 'zero'})")
    assert list(result.keys()) == ["0", "2", "z", "a"]


def test_large_object_one_hundred_keys(ctx: Context) -> None:
    """Forces msgpack map32 header path (>65535 would be excessive;
    >15 is enough to verify we don't corrupt past the fixmap boundary
    where the encoding shape changes)."""
    js = (
        "({"
        + ", ".join(f"k{i}: {i}" for i in range(100))
        + "})"
    )
    result = ctx.eval(js)
    assert len(result) == 100
    assert result["k0"] == 0
    assert result["k99"] == 99
    assert list(result.keys())[:3] == ["k0", "k1", "k2"]


def test_object_containing_uint8array_value(ctx: Context) -> None:
    """Ensures Uint8Array encoding works as a child of an Object, not
    just as a top-level eval result. The Uint8Array branch of the
    encoder is reached via encode_value recursion, so this exercises
    the recursive dispatch rather than the top-level fast path."""
    result = ctx.eval("({payload: new Uint8Array([0, 1, 255])})")
    assert result == {"payload": b"\x00\x01\xff"}


def test_object_containing_bigint_value(ctx: Context) -> None:
    """Similar shape concern for BigInt — ext1 header inside a map
    entry, not a top-level result."""
    big = 10**30
    result = ctx.eval(f"({{count: {big}n}})")
    assert result == {"count": big}


def test_array_of_mixed_types(ctx: Context) -> None:
    """Every encoder branch reachable inside an array: number, string,
    bool, null, undefined, bigint, bytes, nested array, nested object.

    Two asymmetries vs top-level ``ctx.eval`` worth noting:

    - §8: JS numbers are always float64 on the wire, so `1` inside an
      array comes back as `1.0` (Python float). At the top level,
      ``3 == 3.0`` means users rarely notice; inside a list the
      distinction is visible via ``==`` — and that's correct per spec.
    - ``preserve_undefined=False`` coercion only applies at the root
      of ``Context.eval`` (§8). Nested `undefined` values keep the
      ``Undefined`` sentinel so a caller who needs to distinguish
      holes from nulls inside a structure can still see it.
    """
    from quickjs_wasm import UNDEFINED

    result = ctx.eval(
        "[1, 'two', true, null, undefined, 10n**20n,"
        " new Uint8Array([9,8,7]), [1,2], {x: 'y'}]"
    )
    assert result == [
        1.0,
        "two",
        True,
        None,
        UNDEFINED,
        10**20,
        b"\x09\x08\x07",
        [1.0, 2.0],
        {"x": "y"},
    ]


def test_nested_arrays_preserve_shape(ctx: Context) -> None:
    """Array-of-arrays stresses the recursive array header path
    independently of maps. Irregular shapes (jagged) matter more than
    uniform ones."""
    result = ctx.eval("[[1], [2, 3], [4, 5, 6], []]")
    assert result == [[1], [2, 3], [4, 5, 6], []]


def test_uint8array_with_zero_length(ctx: Context) -> None:
    """Edge case: msgpack bin8 header with zero-length body is legal
    but easy to get wrong if a length-check uses `>` where `>=` is
    needed. Verify both the shim and the Python decoder handle it."""
    assert ctx.eval("new Uint8Array(0)") == b""


def test_uint8array_with_single_zero_byte(ctx: Context) -> None:
    """Another edge: a single 0x00 byte in a Uint8Array is easy to
    confuse with a C-string terminator if anyone downstream treats
    the buffer as a string."""
    assert ctx.eval("new Uint8Array([0])") == b"\x00"


def test_objects_across_contexts_do_not_alias() -> None:
    """Marshaling crosses the shim boundary per-context, so two
    contexts on the same runtime produce independent Python values for
    the "same" JS expression. This isn't about identity semantics
    (Python ints are interned anyway); it's about verifying the
    marshaling layer doesn't share scratch state across contexts in a
    way that would corrupt one eval's output with another's."""
    with Runtime() as rt:
        with rt.new_context() as ctx_a, rt.new_context() as ctx_b:
            a = ctx_a.eval("({n: 1, items: [1,2,3]})")
            b = ctx_b.eval("({n: 2, items: [4,5,6]})")
            assert a == {"n": 1, "items": [1, 2, 3]}
            assert b == {"n": 2, "items": [4, 5, 6]}
            # Interleave further evals to guarantee the per-context
            # scratch buffers don't alias — a bug where ctx_b's scratch
            # was shared with ctx_a would corrupt one side once the
            # second eval ran.
            assert ctx_a.eval("({n: 1, items: [1,2,3]})") == a
            assert ctx_b.eval("({n: 2, items: [4,5,6]})") == b
