import struct
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import wasmtime

from quickjs_rs import Runtime
from quickjs_rs._engine import _CUR, _hosted_linker, _Instance, _quickjs_artifact
from quickjs_rs._wasmtime import shared_wasmtime_engine

SECOND = 1_000_000_000


def sequence(clock=None):
    with Runtime(clock=clock) as rt, rt.new_context() as ctx:
        return ctx.eval(
            "[Date.now(), new Date().getTime(), ...Array.from({length: 8}, Math.random)]"
        )


def test_fixed_clock_is_reproducible_across_runtimes():
    a = sequence(lambda: 1234 * SECOND + SECOND // 2)
    b = sequence(lambda: 1234 * SECOND + SECOND // 2)
    assert a == b
    assert a[:2] == [1234500, 1234500]
    # Math.random() is seeded from the clock, so a different clock differs.
    assert sequence(lambda: 99 * SECOND)[2:] != a[2:]


def test_sibling_contexts_share_a_frozen_clock_sequence():
    with Runtime(clock=lambda: 5 * SECOND) as rt:
        with rt.new_context() as a, rt.new_context() as b:
            assert a.eval("Math.random()") == b.eval("Math.random()")
            assert b.eval("Date.now()") == 5000
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: sequence(lambda: n * SECOND), range(4)))
    assert [row[0] for row in results] == [0, 1000, 2000, 3000]


def test_live_clock():
    now = [10_250_000_000]
    with Runtime(clock=lambda: now[0]) as rt, rt.new_context() as ctx:
        assert ctx.eval("Date.now()") == 10250
        now[0] = 20_500_000_000
        assert ctx.eval("new Date().getTime()") == 20500


def test_snapshot_carries_math_random_but_not_the_clock():
    with Runtime(clock=lambda: SECOND) as rt, rt.new_context() as ctx:
        ctx.eval("Math.random(); Math.random()")
        snap = ctx.create_snapshot()
        expected = ctx.eval("[Math.random(), Math.random()]")
    with Runtime(clock=lambda: 9 * SECOND) as rt2, rt2.new_context() as ctx2:
        rt2.restore_snapshot(snap, ctx2)
        assert ctx2.eval("[Math.random(), Math.random()]") == expected
        assert ctx2.eval("Date.now()") == 9000


def test_defaults():
    before = time.time() * 1000
    result = sequence()
    after = time.time() * 1000
    assert before - 1 <= result[0] <= after
    assert all(0 <= x < 1 for x in result[2:])


def test_clock_must_be_callable():
    with pytest.raises(TypeError, match="callable"):
        Runtime(clock=5)


def test_wasi_clocks_and_bounds():
    inst = _Instance(clock=lambda: 12_250_000_000)
    try:
        ptr = inst.c("qjs_alloc", 8)
        for clock_id in range(4):
            assert inst._wasi_clock_time_get(clock_id, 0, ptr) == 0
            assert struct.unpack("<Q", inst.mem.read(inst.store, ptr, ptr + 8))[0] == 12250000000
        assert inst._wasi_clock_time_get(4, 0, ptr) == 28
        for bad in [-1, inst._mem_size() - 7, inst._mem_size()]:
            assert inst._wasi_clock_time_get(0, 0, bad) == 21
    finally:
        inst.close()


@pytest.mark.parametrize("value", [-1, 1 << 64, 1.5, float("nan"), True, None])
def test_invalid_clock_results(value):
    inst = _Instance(clock=lambda: value)
    try:
        ptr = inst.c("qjs_alloc", 8)
        assert inst._wasi_clock_time_get(0, 0, ptr) == 28
    finally:
        inst.close()


def test_clock_errors_surface_as_wasi_errors():
    def fail():
        raise RuntimeError("private host detail")

    inst = _Instance(clock=fail)
    try:
        ptr = inst.c("qjs_alloc", 8)
        assert inst._wasi_clock_time_get(0, 0, ptr) == 29
    finally:
        inst.close()


def test_bundled_clock_import_audit():
    imports = {
        item.name
        for item in _quickjs_artifact().module.imports
        if item.module == "wasi_snapshot_preview1"
    }
    assert "clock_time_get" in imports
    assert "clock_res_get" not in imports
    assert "poll_oneoff" not in imports


def test_default_runtime_uses_the_native_linker():
    artifact = _quickjs_artifact()
    assert _hosted_linker() is not artifact.linker
    engine = shared_wasmtime_engine()
    store = wasmtime.Store(engine)
    store.set_wasi(wasmtime.WasiConfig())
    module = wasmtime.Module(
        engine,
        """(module
        (import "wasi_snapshot_preview1" "clock_time_get"
            (func $clock (param i32 i64 i32) (result i32)))
        (memory (export "memory") 1)
        (func (export "clock") (param i32 i32) (result i32)
            local.get 0 i64.const 0 local.get 1 call $clock))""",
    )
    # No `_CUR.inst` is set here: a shadowed import would fail, native WASI works.
    exports = artifact.linker.instantiate(store, module).exports(store)
    mem = exports["memory"]
    before = time.time_ns()
    assert exports["clock"](store, 0, 8) == 0
    now = struct.unpack("<Q", mem.read(store, 8, 16))[0]
    # Native WASI and `time.time_ns()` read different host clocks; on Windows
    # before Python 3.13 the latter is coarse, so allow some skew.
    slack = 50_000_000
    assert before - slack <= now <= time.time_ns() + slack


def test_hosted_linker_guest_dispatch():
    inst = _Instance(clock=lambda: 4_500_000_000)
    previous = getattr(_CUR, "inst", None)
    try:
        module = wasmtime.Module(
            inst.store.engine,
            """(module
            (import "wasi_snapshot_preview1" "clock_time_get"
                (func $clock (param i32 i64 i32) (result i32)))
            (func (export "clock") (param i32 i32) (result i32)
                local.get 0 i64.const 0 local.get 1 call $clock))""",
        )
        exports = _hosted_linker().instantiate(inst.store, module).exports(inst.store)
        ptr = inst.c("qjs_alloc", 8)
        _CUR.inst = inst
        for clock_id in range(4):
            assert exports["clock"](inst.store, clock_id, ptr) == 0
            assert struct.unpack("<Q", inst.mem.read(inst.store, ptr, ptr + 8))[0] == 4500000000
    finally:
        _CUR.inst = previous
        inst.close()


def test_reentrant_instances_read_their_own_clock():
    with Runtime(clock=lambda: SECOND) as outer_rt, Runtime(clock=lambda: 2 * SECOND) as inner_rt:
        with outer_rt.new_context() as outer, inner_rt.new_context() as inner:

            @outer.function
            def nested():
                return inner.eval("Date.now()")

            assert outer.eval("[Date.now(), nested(), Date.now()]") == [1000, 2000, 1000]
