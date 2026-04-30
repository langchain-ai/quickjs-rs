# Benchmarks

Performance benchmarks for quickjs-rs. See `benchmarks/README.md` for the
full spec — what each benchmark measures, expected order-of-magnitude
ranges, and the rationale behind the structure.

These are **measurements, not tests**. They do not assert behavior.
Correctness lives in `tests/`; benchmarks live here and run under
[pytest-codspeed](https://github.com/CodSpeedHQ/pytest-codspeed) so CI
can track regressions commit-to-commit.

## Running locally

```bash
# Install bench extras (separate from dev extras; includes codspeed + matplotlib)
pip install -e ".[dev,bench]"

# Run every benchmark with wall-time measurement
pytest benchmarks/ --codspeed

# Run CodSpeed memory benchmarks locally
pytest benchmarks/test_memory.py --codspeed --codspeed-mode memory

# Run a single file
pytest benchmarks/test_startup.py --codspeed

# Run a single benchmark by name
pytest benchmarks/test_eval_sync.py::bench_eval_fibonacci_30 --codspeed

# Dry-run (no codspeed, no timing) — useful to verify the benchmark code
# itself runs cleanly before spending minutes on measurement
pytest benchmarks/
```

Memory profiling sweep (CSV + markdown summary):

```bash
python3 benchmarks/memory_experiment.py \
  --output-csv artifacts/memory/memory-profile.csv \
  --output-markdown artifacts/memory/memory-profile.md \
  --output-plots-dir artifacts/memory/plots \
  --output-visual-markdown artifacts/memory/memory-report.md
```

`--codspeed` switches pytest-codspeed into measurement mode. Without it,
benchmark bodies still execute (a good smoke check) but no timing is
recorded.

`benchmarks/memory_experiment.py` is separate from pytest-codspeed: it
profiles runtime/context fan-out memory pressure across configurable mixes
and emits CSV, summary markdown, and optional matplotlib visuals focused on
whole-process RSS and QuickJS counters (residual is inferred by subtraction).
The visual bundle includes scatter/stack/phase plots plus
hypothesis-focused linearity diagnostics (total-context trend, runtime/context
effect lines, and observed-vs-predicted linear fit).

Local runs do **not** need a CodSpeed account — the plugin prints a
results table to stdout. CI uploads runs to CodSpeed for diffing across
commits.

## Layout

| File | Scope (`benchmarks/README.md`) |
|---|---|
| `conftest.py` | Shared fixtures: `rt` (module-scoped), `ctx` (function-scoped), `async_ctx` (owns its Runtime + pre-registers the `instant` async host fn) |
| `test_startup.py` | `Runtime()`, `new_context()`, full cold-start |
| `test_eval_sync.py` | noop, arithmetic, JSON parse, `fib(30)`, loop 1M, regex, object churn |
| `test_marshaling.py` | int, string, dict, list, bytes round-trips |
| `test_host_functions.py` | sync + async host call dispatch |
| `test_eval_async.py` | async pipeline, fan-out, sequential await |
| `test_threaded_stress.py` | Threaded stress: multi-runtime/context isolation under concurrent load + TPS |
| `test_memory.py` | CodSpeed memory mode suite — allocation regression tracking |

## Naming convention

Benchmark functions use the `bench_` prefix; pytest is configured
(`pyproject.toml → [tool.pytest.ini_options] python_functions`) to
collect both `test_*` and `bench_*`. The prefix keeps benchmark functions
visually distinct from correctness tests when scanning output.

## Two timing patterns

Most sync benchmarks use the `benchmark` fixture to time **only** the
operation under test:

```python
def bench_eval_arithmetic(benchmark, ctx):
    benchmark(ctx.eval, "1 + 2")
```

Async benchmarks can't use that fixture (its inner call is synchronous),
so they use `@pytest.mark.benchmark` which times the whole test function.
Fixture setup (Runtime/Context creation) is hoisted into `async_ctx` so
it's excluded from the measurement:

```python
@pytest.mark.benchmark
async def bench_eval_async_noop(async_ctx):
    await async_ctx.eval_async("undefined")
```

## Gotchas

**Top-level `let`/`const` redeclaration.** pytest-codspeed runs each
benchmark body many times on the same `Context`. Top-level `let x = ...`
throws `SyntaxError` on the second iteration. Wrap bodies that declare
variables in an IIFE:

```js
(() => { const s = 'a'.repeat(1000); return /needle/.test(s); })()
```

**Reading the results table.** Use `Run time / Iters` for the per-call
time. pytest-codspeed 4.4.0 appears to double-divide "Time (best)" by
`iter_per_round`, so that column is off by a factor of ~`iter_per_round`
for fast benchmarks.

## CI

`.github/workflows/benchmarks.yml` runs on push to `main` and on PRs.
It invokes CodSpeed twice:

- `mode: walltime` on the full benchmark suite (latency/throughput)
- `mode: memory` on `test_memory.py` (heap allocation regressions)

For capacity planning (runtime/context sweep under a 1 GB cap), use the
manual/weekly `.github/workflows/memory-profiling.yml` job which emits
CSV/markdown artifacts from `benchmarks/memory_experiment.py`.

## When a benchmark lands outside its expected range

`spec/benchmarks.md §8` lists order-of-magnitude targets. If a run lands
outside its range, the rule is: **investigate, don't silently adjust.**
Either the range is wrong and the spec needs an update (separate commit),
or the implementation has an unexpected cost center worth profiling in
v0.3. Known items flagged against the v0.2 baseline are recorded in the
relevant commit bodies.
