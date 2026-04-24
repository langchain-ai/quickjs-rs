# Benchmarks

Performance benchmarks for quickjs-wasm. See `benchmarks/README.md` for the
full spec — what each benchmark measures, expected order-of-magnitude
ranges, and the rationale behind the structure.

These are **measurements, not tests**. They do not assert behavior.
Correctness lives in `tests/`; benchmarks live here and run under
[pytest-codspeed](https://github.com/CodSpeedHQ/pytest-codspeed) so CI
can track regressions commit-to-commit.

## Running locally

```bash
# Install bench extras (separate from dev extras)
pip install -e ".[dev,bench]"

# Run every benchmark with wall-time measurement
pytest benchmarks/ --codspeed

# Run a single file
pytest benchmarks/test_startup.py --codspeed

# Run a single benchmark by name
pytest benchmarks/test_eval_sync.py::bench_eval_fibonacci_30 --codspeed

# Dry-run (no codspeed, no timing) — useful to verify the benchmark code
# itself runs cleanly before spending minutes on measurement
pytest benchmarks/
```

`--codspeed` switches pytest-codspeed into measurement mode. Without it,
benchmark bodies still execute (a good smoke check) but no timing is
recorded.

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
It reuses the `build-wasm` workflow to get a fresh `quickjs.wasm`, then
invokes `CodSpeedHQ/action@v4` with `mode: walltime`. Simulation mode
(Valgrind) can't see wasm execution cost, so wall-time is the only
meaningful mode for this project.
