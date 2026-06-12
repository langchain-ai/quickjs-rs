"""Spike A: epoch interruption under the GIL (go/no-go gate for the Python host).

The WASM hardening spec requires preemptive timeout via Wasmtime epoch
interruption in the Python host (docs/repl-wasm-security-hardening-spec.md,
"CPU And Timeouts" / Phase 1 exit criteria). That mechanism only works if
wasmtime-py releases the GIL while wasm executes: the watchdog is a Python
thread, and if the blocked-in-wasm main thread held the GIL, the watchdog
could never run, no epoch increment would ever happen, and a hostile
`for(;;);` would hang the process forever.

This spike proves or refutes that with no QuickJS involved: a wat module
exporting an infinite loop, a deadline of 1 epoch tick, and a watchdog
thread that increments the epoch after WATCHDOG_DELAY seconds.

PASS = the call traps shortly after WATCHDOG_DELAY, and a second observer
thread demonstrably made progress while the main thread was inside wasm.
FAIL/hang = wasmtime-py holds the GIL across wasm calls; the Python
runtime choice must be re-opened.

Run under an external hard timeout so a GIL-held hang fails cleanly:

    timeout 30 python spikes/epoch_gil_spike.py
"""

import sys
import threading
import time

from wasmtime import Config, Engine, Instance, Module, Store, Trap

WATCHDOG_DELAY = 0.5  # seconds before the watchdog increments the epoch
SLACK = 2.0  # generous scheduling slack for the assertion window

WAT = """
(module
  (func (export "spin")
    (loop $l
      br $l)))
"""


def main() -> int:
    config = Config()
    config.epoch_interruption = True
    engine = Engine(config)
    module = Module(engine, WAT)
    store = Store(engine)
    store.set_epoch_deadline(1)
    instance = Instance(store, module, [])
    spin = instance.exports(store)["spin"]

    # Observer: counts while the main thread is inside wasm. Any progress
    # here is direct proof the GIL is released during wasm execution.
    progress = {"ticks": 0}
    observing = threading.Event()
    observing.set()

    def observer() -> None:
        while observing.is_set():
            progress["ticks"] += 1
            time.sleep(0.01)

    def watchdog() -> None:
        time.sleep(WATCHDOG_DELAY)
        engine.increment_epoch()

    threading.Thread(target=observer, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    start = time.monotonic()
    try:
        spin(store)
    except Trap as trap:
        elapsed = time.monotonic() - start
        observing.clear()
        print(f"trap raised after {elapsed:.3f}s (watchdog delay {WATCHDOG_DELAY}s)")
        print(f"trap message: {trap.message}")
        print(f"observer ticks while main thread was blocked in wasm: {progress['ticks']}")
        if elapsed > WATCHDOG_DELAY + SLACK:
            print("FAIL: trap fired far later than the watchdog delay")
            return 1
        if progress["ticks"] == 0:
            print("FAIL: observer thread made no progress; GIL appears held")
            return 1
        print("PASS: epoch trap fired while the main Python thread was blocked "
              "inside the wasm call; wasmtime-py releases the GIL during execution")
        return 0

    observing.clear()
    print("FAIL: spin() returned without trapping")
    return 1


if __name__ == "__main__":
    sys.exit(main())
