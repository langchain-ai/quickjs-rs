// Spike B: Node worker-hosted instance + SharedArrayBuffer interrupt flag.
//
// Validates the default Node deployment shape from the hardening spec
// ("CPU And Timeouts"): the wasm instance lives in a worker_threads worker,
// the main thread flips an interrupt flag in a SharedArrayBuffer mid-eval,
// and the guest observes it through a host import (a JS closure in the
// worker reading the SAB via Atomics.load) — no shared wasm memory, no
// threads feature, plain wasm32-class module. Worker termination is the
// backstop for a loop that never reaches an interrupt check.
//
// PASS = the cooperative loop is interrupted ~500ms in (flag flip observed
// mid-eval from outside the worker), and the hostile loop is killed by
// terminate() at the backstop deadline.
//
// Run: node spikes/node_worker_interrupt_spike.mjs
// (expects interrupt_spike.wasm next to this file; rebuild from the .wat
// with: python -c "import wasmtime; open('spikes/interrupt_spike.wasm','wb')\
// .write(wasmtime.wat2wasm(open('spikes/interrupt_spike.wat').read()))")

import { Worker, isMainThread, workerData, parentPort } from 'node:worker_threads';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const FLAG_DELAY_MS = 500; // when the main thread flips the interrupt flag
const BACKSTOP_MS = 1500; // when the main thread gives up and terminates

if (isMainThread) {
  const run = (mode) =>
    new Promise((resolve) => {
      const sab = new SharedArrayBuffer(4);
      const flag = new Int32Array(sab);
      const worker = new Worker(fileURLToPath(import.meta.url), { workerData: { sab, mode } });
      const started = Date.now();
      let settled = false;
      worker.on('message', (msg) => {
        settled = true;
        resolve({ ...msg, wallMs: Date.now() - started });
        worker.terminate();
      });
      setTimeout(() => Atomics.store(flag, 0, 1), FLAG_DELAY_MS);
      setTimeout(() => {
        if (!settled) {
          worker
            .terminate()
            .then(() => resolve({ mode, terminated: true, wallMs: Date.now() - started }));
        }
      }, BACKSTOP_MS);
    });

  const cooperative = await run('cooperative');
  console.log('cooperative:', JSON.stringify(cooperative));
  const hostile = await run('hostile');
  console.log('hostile:', JSON.stringify(hostile));

  const cooperativeOk =
    !cooperative.terminated &&
    cooperative.wallMs >= FLAG_DELAY_MS - 50 &&
    cooperative.wallMs < BACKSTOP_MS - 100;
  const hostileOk = hostile.terminated === true;
  if (cooperativeOk && hostileOk) {
    console.log(
      'PASS: SAB flag interrupted the cooperative loop mid-eval from the main thread; ' +
        'worker termination killed the hostile loop'
    );
    process.exit(0);
  }
  console.log('FAIL: cooperativeOk=%s hostileOk=%s', cooperativeOk, hostileOk);
  process.exit(1);
} else {
  const { sab, mode } = workerData;
  const flag = new Int32Array(sab);
  const bytes = readFileSync(new URL('./interrupt_spike.wasm', import.meta.url));
  const { instance } = await WebAssembly.instantiate(bytes, {
    host: { interrupt: () => Atomics.load(flag, 0) },
  });
  if (mode === 'cooperative') {
    const start = Date.now();
    const iters = instance.exports.spin_cooperative();
    parentPort.postMessage({ mode, iters, evalMs: Date.now() - start });
  } else {
    instance.exports.spin_hostile(); // never returns; only terminate() ends this
  }
}
