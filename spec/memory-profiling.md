# quickjs-rs memory profiling spec

Status: draft implementation-ready

## 1. Goals

Answer, with reproducible data:

1. What memory pressure comes from runtime/context fan-out in one process?
2. Are runtime/context objects being reclaimed when closed / garbage-collected?
3. Under a 1 GB process cap, what is the upper bound for each runtime/context mix?
4. What visualization/reporting path should be used in local dev and CI?

## 2. Instrumentation surface

The runtime API now exposes:

- `Runtime.memory_usage()` -> QuickJS `JS_ComputeMemoryUsage` counters.
- `Runtime.run_gc()` -> explicit QuickJS cycle GC (`JS_RunGC`).

Primary counters:

- `malloc_size` (QuickJS-allocator bytes in use)
- `memory_used_size` (effective bytes used by JS values)
- `malloc_limit` (runtime-level cap configured via `memory_limit`)

Process-level counter:

- RSS (from `/proc/self/status` when available; fallback to `ru_maxrss` peak)

## 3. Experiment matrix

Use `benchmarks/memory_experiment.py` to sweep:

- `runtimes`: e.g. `1,2,4,8,12,16`
- `contexts_per_runtime`: e.g. `1,2,4,8`
- `memory_limit_mb`: e.g. `16,32,64` (`0` means unlimited)
- `payload_mb_per_context`: e.g. `0,1,4` (pinned `Uint8Array` payload)

Each config records:

- baseline RSS
- RSS after runtime/context spawn
- RSS + QuickJS counters after payload load
- RSS + QuickJS counters after `payload clear + run_gc`
- RSS after `context.close + runtime.close + Python gc.collect`
- peak process RSS (`ru_maxrss`) for whole-process envelope tracking
- error state (if allocation or creation fails)

Outputs:

- CSV (`artifacts/memory/memory-profile.csv`)
- Optional markdown summary (`artifacts/memory/memory-profile.md`)
- Optional matplotlib report from the same script:
  - `benchmarks/memory_experiment.py --output-plots-dir ... --output-visual-markdown ...`
  - plots + markdown in `artifacts/memory/plots/` and `artifacts/memory/memory-report.md`

## 4. GC/reclamation checks

GC correctness is evaluated in two steps:

1. Logical release:
- clear pinned globals in every context
- call `Runtime.run_gc()` on each runtime
- verify post-GC counters are captured and stable across runs

2. Lifecycle release:
- close all contexts, then runtimes
- run Python `gc.collect()`
- record post-close RSS

The key signal is not a single absolute value, but the shape:

- `after_gc` should not trend upward across repeated identical runs
- `after_close` should return close to baseline (allowing allocator retention)

## 5. 1 GB cap modeling

Let:

- `CAP = 1_024 MiB`
- `B = baseline_rss_bytes`
- `R = (after_payload_rss_bytes - B) / runtimes` for one measured mix

Then:

- Observed same-mix bound:
  - `max_runtimes_same_mix = floor((CAP - B) / R)`
- Hard optimistic bound from configured per-runtime limits:
  - `max_runtimes_if_all_hit_limit = floor(CAP / memory_limit_bytes)`

Use both:

- `same_mix` tells what this workload can sustain
- `if_all_hit_limit` tells an upper cap if every runtime saturates its limit simultaneously

The practical bound is the minimum of those two when `memory_limit > 0`.

## 6. Visualization and reporting

1. Jupyter notebook path
- Load the CSV and chart:
  - RSS vs runtime/context grid
  - QuickJS `malloc_size` vs payload
  - `after_payload` minus `after_gc` deltas

2. GitHub Actions path
- CodSpeed-first:
  - `.github/workflows/benchmarks.yml` runs memory benchmarks with `mode: memory`
  - benchmark file: `benchmarks/test_memory.py`
- Capacity sweep:
  - `.github/workflows/memory-profiling.yml` runs weekly and on `workflow_dispatch`
  - runs reduced `benchmarks/memory_experiment.py` matrix for envelope tracking
  - enables `memory_experiment.py` plot/report flags to publish matplotlib visuals
- Upload CSV/markdown as artifacts
- append both summary + visual-report markdown to `$GITHUB_STEP_SUMMARY`

3. CodSpeed path
- `pytest-codspeed` supports `memory` mode (allocation-focused instrument)
- use selected deterministic benchmark subsets for regression tracking
- do not use as a replacement for full matrix capacity sweeps

## 7. Suggested rollout

1. Keep CodSpeed memory benchmarks as the default memory status signal.
2. Keep reduced weekly/manual sweep for capacity envelopes and 1 GB planning.
3. Track both in PR/release review:
   - CodSpeed: regression deltas
   - Sweep artifacts: absolute envelope changes
4. Add a notebook (or script) that produces trend charts from CSV history.
