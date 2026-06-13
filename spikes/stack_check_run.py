"""Stack-check verdict: does unbounded JS recursion surface as a catchable
error, or trap the whole instance?

Calls qrs_recurse_depth under a bounded wasm stack. Three outcomes:
  - positive return  -> recursion is CATCHABLE at that depth (limit fired)
  - negative return  -> engine surfaced a non-Range error (still catchable,
                        instance alive)
  - wasmtime Trap    -> wasm stack exhausted; instance is DEAD (the
                        "permanently fatal" case the spec warns about)

Usage: python spikes/stack_check_run.py <path-to.wasm> [max_wasm_stack_bytes]
"""

import sys

from wasmtime import Config, Engine, Linker, Module, Store, Trap, WasiConfig


def main() -> int:
    wasm_path = sys.argv[1]
    max_stack = int(sys.argv[2]) if len(sys.argv) > 2 else 1 << 20  # 1 MiB

    config = Config()
    # Bound the host wasm stack so runaway recursion traps deterministically
    # rather than risking a real native overflow.
    config.max_wasm_stack = max_stack
    engine = Engine(config)
    module = Module.from_file(engine, wasm_path)

    store = Store(engine)
    wasi = WasiConfig()
    wasi.inherit_stdout()
    wasi.inherit_stderr()
    store.set_wasi(wasi)
    linker = Linker(engine)
    linker.define_wasi()
    instance = linker.instantiate(store, module)
    recurse = instance.exports(store)["qrs_recurse_depth"]

    # limit arg is the QuickJS set_max_stack_size value (bytes).
    try:
        depth = recurse(store, 256 * 1024)
    except Trap as t:
        print(f"max_wasm_stack={max_stack}: TRAP — instance dead")
        print(f"  {str(t).splitlines()[0]}")
        print("VERDICT: recursion is NOT catchable by default — fatal trap")
        return 2

    if depth > 0:
        print(f"max_wasm_stack={max_stack}: caught at depth {depth}")
        print("VERDICT: recursion IS catchable — QuickJS stack check fired, "
              "instance survives")
        return 0
    print(f"max_wasm_stack={max_stack}: returned sentinel {depth} "
          "(engine error, but no trap — instance survives)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
