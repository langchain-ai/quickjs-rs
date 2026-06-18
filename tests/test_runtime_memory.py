"""Runtime memory introspection + GC hooks."""

from __future__ import annotations

import pytest

from quickjs_rs import QuickJSError, Runtime

_MEMORY_USAGE_KEYS = {
    "malloc_size",
    "malloc_limit",
    "memory_used_size",
    "malloc_count",
    "memory_used_count",
    "atom_count",
    "atom_size",
    "str_count",
    "str_size",
    "obj_count",
    "obj_size",
    "prop_count",
    "prop_size",
    "shape_count",
    "shape_size",
    "js_func_count",
    "js_func_size",
    "js_func_code_size",
    "js_func_pc2line_count",
    "js_func_pc2line_size",
    "c_func_count",
    "array_count",
    "fast_array_count",
    "fast_array_elements",
    "binary_object_count",
    "binary_object_size",
}


def test_runtime_memory_usage_shape() -> None:
    with Runtime(memory_limit=64 * 1024 * 1024) as rt:
        usage = rt.memory_usage()
        assert _MEMORY_USAGE_KEYS.issubset(usage.keys())
        assert all(isinstance(v, int) for v in usage.values())
        assert usage["malloc_limit"] >= 0


def test_runtime_run_gc_callable_under_load() -> None:
    with Runtime(memory_limit=64 * 1024 * 1024) as rt:
        with rt.new_context() as ctx:
            # Allocate and pin data, then drop it and force cycle GC.
            ctx.eval(
                """
                (() => {
                    const payload = [];
                    for (let i = 0; i < 128; i++) payload.push(new Uint8Array(64 * 1024));
                    globalThis.__payload = payload;
                    return payload.length;
                })()
                """
            )
            _ = rt.memory_usage()
            ctx.eval("globalThis.__payload = undefined")
            rt.run_gc()
            _ = rt.memory_usage()


def test_runtime_memory_usage_empty_is_zeros() -> None:
    """A runtime with no live context has no heap; memory_usage reports
    all-zeros with the full field set (stable shape, honest empty answer)."""
    with Runtime() as rt:
        usage = rt.memory_usage()
        assert _MEMORY_USAGE_KEYS.issubset(usage.keys())
        assert all(v == 0 for v in usage.values())


def test_runtime_memory_usage_aggregates_across_contexts() -> None:
    """Each context is its own heap; runtime memory_usage sums across them.
    Two contexts each holding a payload report more obj_count than one."""
    with Runtime(memory_limit=64 * 1024 * 1024) as rt:
        with rt.new_context() as c1:
            c1.eval("globalThis.__p = Array.from({length: 500}, (_, i) => ({i}))")
            one_ctx = rt.memory_usage()
            with rt.new_context() as c2:
                c2.eval("globalThis.__p = Array.from({length: 500}, (_, i) => ({i}))")
                two_ctx = rt.memory_usage()
        # Adding a second live context with its own objects strictly increases
        # the aggregated object count.
        assert two_ctx["obj_count"] > one_ctx["obj_count"]


def test_runtime_memory_api_raises_after_close() -> None:
    rt = Runtime()
    rt.close()
    with pytest.raises(QuickJSError):
        rt.run_gc()
    with pytest.raises(QuickJSError):
        rt.memory_usage()
