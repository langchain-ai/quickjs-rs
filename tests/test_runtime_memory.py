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


def test_runtime_memory_api_raises_after_close() -> None:
    rt = Runtime()
    rt.close()
    with pytest.raises(QuickJSError):
        rt.run_gc()
    with pytest.raises(QuickJSError):
        rt.memory_usage()
