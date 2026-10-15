import hashlib
import struct
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import wasmtime

from quickjs_rs import Runtime
from quickjs_rs._engine import _CUR, _Instance, _quickjs_artifact


def sequence(clock=None, seed=None):
    with Runtime(clock=clock, random_seed=seed) as rt, rt.new_context() as ctx:
        return ctx.eval(
            "[Date.now(), new Date().getTime(), ...Array.from({length: 8}, Math.random)]"
        )


def test_fixed_clock_and_default_rng():
    assert sequence(lambda: 1234.5) == sequence(lambda: 1234.5)
    assert sequence(lambda: 1234.5)[:2] == [1234500, 1234500]


def test_seed_is_clock_independent():
    a = sequence(lambda: 1, 42)
    b = sequence(lambda: 200, 42)
    assert a[2:] == b[2:]
    assert a[2:] != sequence(lambda: 1, 43)[2:]
    assert sequence(lambda: 1, 0)[2:] == sequence(lambda: 1, 1)[2:]
    assert sequence(None, (1 << 64) - 1)[2:] == sequence(None, (1 << 64) - 1)[2:]


def test_context_streams_are_independent():
    with Runtime(clock=lambda: 5, random_seed=42) as rt:
        with rt.new_context() as a, rt.new_context() as b:
            first = a.eval("Math.random()")
            a.eval("Math.random()")
            assert b.eval("Math.random()") == first
            assert b.eval("Date.now()") == 5000
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: sequence(lambda: float(n), n), range(4)))
    assert [row[0] for row in results] == [0, 1000, 2000, 3000]
    assert len({row[2] for row in results}) == 3


def test_live_clock():
    now = [10.25]
    with Runtime(clock=lambda: now[0]) as rt, rt.new_context() as ctx:
        assert ctx.eval("Date.now()") == 10250
        now[0] = 20.5
        assert ctx.eval("new Date().getTime()") == 20500


def test_defaults():
    before = time.time() * 1000
    result = sequence()
    after = time.time() * 1000
    assert before - 1 <= result[0] <= after
    assert all(0 <= x < 1 for x in result[2:])
    assert sequence()[2:] != result[2:]


@pytest.mark.parametrize("seed", [-1, 1 << 64, 1.2, True])
def test_invalid_seed(seed):
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        Runtime(random_seed=seed)


def test_wasi_clocks_entropy_and_bounds():
    a = _Instance(clock=lambda: 12.25, random_seed=42)
    b = _Instance(clock=lambda: 99, random_seed=42)
    try:
        ptr = a.c("qjs_alloc", 80)
        other = b.c("qjs_alloc", 80)
        for clock_id in range(4):
            assert a._wasi_call("clock_time_get", clock_id, 0, ptr) == 0
            assert struct.unpack("<Q", a.mem.read(a.store, ptr, ptr + 8))[0] == 12250000000
            assert a._wasi_call("clock_res_get", clock_id, ptr) == 0
            assert struct.unpack("<Q", a.mem.read(a.store, ptr, ptr + 8))[0] == 1
        assert a._wasi_call("clock_time_get", 4, 0, ptr) == 28
        for bad in [-1, a._mem_size() - 7, a._mem_size()]:
            assert a._wasi_call("clock_time_get", 0, 0, bad) == 21
            assert a._wasi_call("clock_res_get", 0, bad) == 21
        assert a._wasi_call("random_get", -1, 8) == 21
        assert a._wasi_call("random_get", ptr, -1) == 21
        assert a._entropy_offset == 0
        assert a._wasi_call("random_get", ptr, 13) == 0
        assert a._wasi_call("random_get", ptr + 13, 51) == 0
        assert b._wasi_call("random_get", other, 64) == 0
        data = bytes(a.mem.read(a.store, ptr, ptr + 64))
        assert data == bytes(b.mem.read(b.store, other, other + 64))
        assert (
            data[:32]
            == hashlib.sha256(
                b"quickjs-rs/wasi-random/v1\0" + (42).to_bytes(8, "little") + bytes(16)
            ).digest()
        )
        assert a._wasi_call("poll_oneoff", 0, 0, 1, 0) == 58
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 1e30])
def test_invalid_clock_results(value):
    inst = _Instance(clock=lambda: value)
    try:
        ptr = inst.c("qjs_alloc", 8)
        assert inst._wasi_call("clock_time_get", 0, 0, ptr) == 28
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
    assert {name for name in imports if "clock" in name or "poll" in name} <= {
        "clock_time_get",
        "clock_res_get",
        "poll_oneoff",
    }


def test_native_wasi_fallback():
    inst = _Instance()
    try:
        ptr = inst.c("qjs_alloc", 64)
        assert inst._wasi_call("random_get", ptr, 64) == 0
        assert bytes(inst.mem.read(inst.store, ptr, ptr + 64)) != bytes(64)
        assert inst._wasi_call("clock_res_get", 0, ptr) == 0
        assert struct.unpack("<Q", inst.mem.read(inst.store, ptr, ptr + 8))[0] > 0
    finally:
        inst.close()


def test_shared_linker_guest_dispatch():
    inst = _Instance(clock=lambda: 4.5, random_seed=9)
    previous = getattr(_CUR, "inst", None)
    try:
        artifact = _quickjs_artifact()
        module = wasmtime.Module(
            inst.store.engine,
            """(module
            (import "wasi_snapshot_preview1" "clock_time_get"
                (func $clock (param i32 i64 i32) (result i32)))
            (import "wasi_snapshot_preview1" "random_get"
                (func $random (param i32 i32) (result i32)))
            (func (export "clock") (param i32 i32) (result i32)
                local.get 0 i64.const 0 local.get 1 call $clock)
            (func (export "random") (param i32 i32) (result i32)
                local.get 0 local.get 1 call $random))""",
        )
        exports = artifact.linker.instantiate(inst.store, module).exports(inst.store)
        ptr = inst.c("qjs_alloc", 32)
        _CUR.inst = inst
        for clock_id in range(4):
            assert exports["clock"](inst.store, clock_id, ptr) == 0
            assert struct.unpack("<Q", inst.mem.read(inst.store, ptr, ptr + 8))[0] == 4500000000
        assert exports["random"](inst.store, ptr, 32) == 0
        assert exports["random"](inst.store, -1, 32) == 21
    finally:
        _CUR.inst = previous
        inst.close()


def test_clock_callback_failure_and_reentrant_instance():
    def fail():
        raise RuntimeError("private host detail")

    inst = _Instance(clock=fail)
    try:
        ptr = inst.c("qjs_alloc", 8)
        assert inst._wasi_call("clock_time_get", 0, 0, ptr) == 29
    finally:
        inst.close()
    with Runtime(clock=lambda: 1, random_seed=42) as outer_rt:
        with Runtime(clock=lambda: 2, random_seed=43) as inner_rt:
            with outer_rt.new_context() as outer, inner_rt.new_context() as inner:

                @outer.function
                def nested():
                    return inner.eval("Date.now()")

                assert outer.eval("[Date.now(), nested(), Date.now()]") == [1000, 2000, 1000]
