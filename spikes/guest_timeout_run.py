"""Phase 1 timeout check: a hostile `while(true){}` is caught gracefully via
the host_interrupt import, returns Timeout status, and the instance SURVIVES
(a subsequent eval still works). Plus the normal-eval path with the import
supplied.

This exercises the graceful timeout tier end-to-end through the real ABI.

Run: python spikes/guest_timeout_run.py <path-to quickjs_core.wasm>
"""
import struct
import sys

from wasmtime import Engine, Func, FuncType, Linker, Module, Store, ValType, WasiConfig


def u32(n):
    return struct.pack("<I", n)


def envelope_eval(src):
    b = src.encode()
    payload = bytes([0x06]) + u32(len(b)) + b
    return u32(1) + struct.pack("<Q", 1) + u32(0) + u32(0) + payload


def main():
    wasm_path = sys.argv[1]
    engine = Engine()
    module = Module.from_file(engine, wasm_path)
    store = Store(engine)
    wasi = WasiConfig()
    wasi.inherit_stdout()
    wasi.inherit_stderr()
    store.set_wasi(wasi)
    linker = Linker(engine)
    linker.define_wasi()

    # Host interrupt flag: the watchdog-free version — we flip it after N polls
    # to simulate a deadline, proving the graceful catch without real timing.
    state = {"polls": 0, "trip_after": 100_000}

    def host_interrupt():
        state["polls"] += 1
        return 1 if state["polls"] >= state["trip_after"] else 0

    linker.define(
        store, "env", "host_interrupt",
        Func(store, FuncType([], [ValType.i32()]), lambda *a: host_interrupt()),
    )

    inst = linker.instantiate(store, module)
    ex = inst.exports(store)
    mem = ex["memory"]

    def call(n, *a):
        return ex[n](store, *a)

    def desc(out):
        return struct.unpack("<IIII", bytes(mem.read(store, out, out + 16)))

    out = call("qrs_alloc", 16, 4)
    call("qrs_runtime_new", 0, 0, out)
    rt = int(struct.unpack("<d", bytes(mem.read(store, desc(out)[2] + 1, desc(out)[2] + 9)))[0])
    s, t, p, l = desc(out)
    call("qrs_response_free", p, l)
    call("qrs_context_new", rt, out)
    s, t, p, l = desc(out)
    ctx = int(struct.unpack("<d", bytes(mem.read(store, p + 1, p + 9)))[0])
    call("qrs_response_free", p, l)
    print(f"runtime {rt}, context {ctx}")

    def do_eval(src):
        env = envelope_eval(src)
        req = call("qrs_alloc", len(env), 1)
        mem.write(store, env, req)
        call("qrs_eval", ctx, req, len(env), out)
        s, t, p, l = desc(out)
        call("qrs_free", req, len(env), 1)
        if l:
            call("qrs_response_free", p, l)
        return s

    # 1. hostile infinite loop -> should hit the interrupt -> Timeout (status 10)
    status = do_eval("while(true){}")
    print(f"infinite-loop eval status = {status} (expect 10 timeout)")
    timeout_ok = status == 10

    # 2. instance survives: a normal eval after the timeout still works
    state["polls"] = 0  # reset the simulated deadline
    status2 = do_eval("1 + 2")
    print(f"post-timeout eval status = {status2} (expect 0 ok)")
    survives_ok = status2 == 0

    if timeout_ok and survives_ok:
        print("PASS: graceful timeout via host_interrupt; instance survived")
        return 0
    print(f"FAIL: timeout_ok={timeout_ok} survives_ok={survives_ok}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
