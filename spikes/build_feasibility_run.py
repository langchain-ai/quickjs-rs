"""Build-feasibility verdict: instantiate the freshly built quickjs-core.wasm
with a zero-capability WASI and confirm the in-module QuickJS engine evals
1 + 2.

A return of 3 from qrs_selftest proves rquickjs + QuickJS run inside a
wasm32-wasip1 module built from source we control on today's toolchain —
the feasibility verdict the lost-artifact reconnaissance could not give.

Run: python spikes/build_feasibility_run.py <path-to.wasm>
"""

import sys

from wasmtime import Engine, Linker, Module, Store, WasiConfig


def main() -> int:
    wasm_path = sys.argv[1]
    engine = Engine()
    module = Module.from_file(engine, wasm_path)

    store = Store(engine)
    # Zero ambient capabilities: no preopened dirs, no inherited env, no
    # network. stdout/stderr inherited only so a guest panic is visible.
    wasi = WasiConfig()
    wasi.inherit_stdout()
    wasi.inherit_stderr()
    store.set_wasi(wasi)

    linker = Linker(engine)
    linker.define_wasi()
    instance = linker.instantiate(store, module)
    exports = instance.exports(store)

    abi = exports["qrs_abi_version"](store)
    result = exports["qrs_selftest"](store)
    print(f"qrs_abi_version() = {abi}")
    print(f"qrs_selftest()    = {result}  (expected 3)")

    if result == 3:
        print("PASS: rquickjs + QuickJS eval '1 + 2' inside a freshly built "
              "wasm32-wasip1 module — feasibility verdict GREEN")
        return 0
    print(f"FAIL: selftest returned {result} "
          "(-1 runtime, -2 context, -3 eval failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
