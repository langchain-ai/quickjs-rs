// Validates the spec's placement-rule / delivery claim (CPU And Timeouts):
// a hostile synchronous wasm spin on a single-threaded JS host freezes the
// event loop, and the cooperative interrupt flag is UNDELIVERABLE because
// nothing on that one thread can run to set it. The worker-hosted control
// shows the main thread staying responsive and the loop being killable.
//
// Reuses spikes/interrupt_spike.wasm:
//   spin_cooperative -> exits when the host.interrupt import returns nonzero
//   spin_hostile     -> loops forever, never calls the import
//
// Each main-thread case runs in a short-lived CHILD process with a hard
// kill, because by definition a frozen main thread cannot end itself — the
// freeze IS the result, captured as the child being killed by its watchdog.
//
// Run: node spikes/main_thread_freeze_spike.mjs

import { Worker, isMainThread, parentPort, workerData } from 'node:worker_threads';
import { fork } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const WASM = new URL('./interrupt_spike.wasm', import.meta.url);
const SELF = fileURLToPath(import.meta.url);

// ── child role: run a main-thread spin and report heartbeats ──────────────
// argv[2] = "hostile" | "cooperative". Prints heartbeat/flag events as it
// goes; the parent kills it and inspects what was printed.
if (process.argv[2] === 'child') {
  const mode = process.argv[3];
  const sab = new SharedArrayBuffer(4);
  const flag = new Int32Array(sab);
  const bytes = readFileSync(WASM);
  const { instance } = await WebAssembly.instantiate(bytes, {
    host: { interrupt: () => Atomics.load(flag, 0) },
  });

  // A heartbeat that SHOULD fire ~every 50ms if the event loop is alive.
  let beats = 0;
  setInterval(() => { beats++; process.stdout.write(`beat ${beats}\n`); }, 50);
  // A timer that SHOULD set the interrupt flag at 200ms — the only thing
  // that could save a cooperative loop. If it never prints, it never ran.
  setTimeout(() => { Atomics.store(flag, 0, 1); process.stdout.write('flag-set\n'); }, 200);

  // Give the timers a tick to arm, then enter the synchronous spin.
  setTimeout(() => {
    process.stdout.write('enter-spin\n');
    const fn = mode === 'hostile' ? instance.exports.spin_hostile
                                  : instance.exports.spin_cooperative;
    fn();
    process.stdout.write('spin-returned\n'); // only prints if it ever exits
  }, 10);
  // keep process alive for the spin
  setInterval(() => {}, 1000);
} else if (!isMainThread) {
  // ── worker role: host the hostile spin for the control case ──────────────
  const bytes = readFileSync(WASM);
  const flag = new Int32Array(workerData.sab);
  const { instance } = await WebAssembly.instantiate(bytes, {
    host: { interrupt: () => Atomics.load(flag, 0) },
  });
  parentPort.postMessage('worker-spinning');
  instance.exports.spin_hostile(); // never returns; parent terminates us
} else {
  // ── main role: orchestrate the three experiments ─────────────────────────
  const runChild = (mode, killAfter) => new Promise((resolve) => {
    const child = fork(SELF, ['child', mode], { stdio: ['ignore', 'pipe', 'inherit', 'ipc'] });
    let out = '';
    child.stdout.on('data', (d) => { out += d; });
    const timer = setTimeout(() => child.kill('SIGKILL'), killAfter);
    child.on('exit', () => {
      clearTimeout(timer);
      const beats = (out.match(/beat /g) || []).length;
      resolve({
        enteredSpin: out.includes('enter-spin'),
        beatsDuringSpin: beats,                 // heartbeats after entering spin
        flagTimerRan: out.includes('flag-set'), // did the interrupt-setter run?
        spinReturned: out.includes('spin-returned'),
      });
    });
  });

  console.log('[1] main-thread HOSTILE spin:');
  const hostile = await runChild('hostile', 1500);
  console.log('   ', JSON.stringify(hostile));

  console.log('[2] main-thread COOPERATIVE spin (flag set from a main-thread timer):');
  const coop = await runChild('cooperative', 1500);
  console.log('   ', JSON.stringify(coop));

  console.log('[3] worker-hosted HOSTILE spin (control):');
  const control = await (async () => {
    const sab = new SharedArrayBuffer(4);
    const worker = new Worker(SELF, { workerData: { sab } });
    let beats = 0;
    const hb = setInterval(() => { beats++; }, 50);
    await new Promise((r) => worker.once('message', r)); // worker-spinning
    await new Promise((r) => setTimeout(r, 500));         // let main thread breathe
    const beatsWhileSpinning = beats;
    await worker.terminate();
    clearInterval(hb);
    return { beatsWhileSpinning, terminated: true };
  })();
  console.log('   ', JSON.stringify(control));

  // ── verdict ──────────────────────────────────────────────────────────────
  const frozeHostile = hostile.enteredSpin && hostile.beatsDuringSpin === 0
    && !hostile.flagTimerRan && !hostile.spinReturned;
  const frozeCoop = coop.enteredSpin && coop.beatsDuringSpin === 0
    && !coop.flagTimerRan && !coop.spinReturned;
  const workerOk = control.beatsWhileSpinning > 0 && control.terminated;

  console.log('\nVERDICT:');
  console.log(`  main-thread hostile froze the event loop:      ${frozeHostile}`);
  console.log(`  main-thread cooperative ALSO unsavable:        ${frozeCoop}`);
  console.log(`  worker control stayed responsive + killable:   ${workerOk}`);
  if (frozeHostile && frozeCoop && workerOk) {
    console.log('PASS: claim validated — a synchronous spin freezes a single-threaded host and the\n'
      + '      cooperative flag is undeliverable there; the worker shape stays responsive.');
    process.exit(0);
  }
  console.log('FAIL: see flags above');
  process.exit(1);
}
