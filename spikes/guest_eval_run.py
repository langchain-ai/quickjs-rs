"""End-to-end Phase 1 guest check: drive quickjs-core.wasm through the real
qrs_* ABI to eval `1 + 2` and read back the wire result.

Exercises the whole bridge: alloc -> write envelope -> qrs_eval -> read
AbiResponse descriptor -> decode payload. Mirrors what the Python host adapter
will eventually do, in a few dozen lines.

Run: python spikes/guest_eval_run.py <path-to quickjs_core.wasm>
"""
import struct
import sys

from wasmtime import Engine, Linker, Module, Store, WasiConfig


def u32(n):
    return struct.pack("<I", n)


def encode_value_string(s):
    b = s.encode("utf-8")
    return bytes([0x06]) + u32(len(b)) + b  # tag String, len, utf8


def encode_envelope(abi_version, request_id, kind, flags, payload_bytes):
    return (
        u32(abi_version)
        + struct.pack("<Q", request_id)  # u64
        + u32(kind)
        + u32(flags)
        + payload_bytes
    )


def decode_value(buf):
    # minimal decoder for the result we expect (Number / Error)
    tag = buf[0]
    if tag == 0x04:
        bits = struct.unpack("<Q", buf[1:9])[0]
        return ("Number", struct.unpack("<d", struct.pack("<Q", bits))[0])
    if tag == 0x00:
        return ("Null", None)
    if tag == 0x0B:
        return ("Error", buf)
    return (f"tag{tag:#x}", buf)


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
    inst = linker.instantiate(store, module)
    ex = inst.exports(store)
    mem = ex["memory"]

    def call(name, *args):
        return ex[name](store, *args)

    def read(ptr, n):
        return bytes(mem.read(store, ptr, ptr + n))

    def write(ptr, data):
        mem.write(store, data, ptr)

    def read_descriptor(out):
        d = read(out, 16)
        status, tag, ptr, length = struct.unpack("<IIII", d)
        return status, tag, ptr, length

    print("abi_version:", call("qrs_abi_version"))

    # out region for descriptors
    out = call("qrs_alloc", 16, 4)

    # runtime new (empty request)
    call("qrs_runtime_new", 0, 0, out)
    status, tag, ptr, length = read_descriptor(out)
    assert status == 0, f"runtime_new status {status}"
    rt_id = int(decode_value(read(ptr, length))[1])
    call("qrs_response_free", ptr, length)
    print("runtime id:", rt_id)

    # context new
    call("qrs_context_new", rt_id, out)
    status, tag, ptr, length = read_descriptor(out)
    assert status == 0, f"context_new status {status}"
    ctx_id = int(decode_value(read(ptr, length))[1])
    call("qrs_response_free", ptr, length)
    print("context id:", ctx_id)

    # eval "1 + 2"
    env = encode_envelope(1, 1, 0, 0, encode_value_string("1 + 2"))
    req = call("qrs_alloc", len(env), 1)
    write(req, env)
    call("qrs_eval", ctx_id, req, len(env), out)
    status, tag, ptr, length = read_descriptor(out)
    call("qrs_free", req, len(env), 1)
    print(f"eval status={status} tag={tag} len={length}")
    assert status == 0, f"eval status {status} (expected 0 ok)"
    kind, val = decode_value(read(ptr, length))
    call("qrs_response_free", ptr, length)
    print(f"eval result: {kind} = {val}")

    if kind == "Number" and val == 3.0:
        print("PASS: guest evaluated 1 + 2 = 3 through the real qrs_* ABI")
        return 0
    print("FAIL: unexpected result")
    return 1


if __name__ == "__main__":
    sys.exit(main())
