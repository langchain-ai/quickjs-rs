# quickjs-wasm benchmark spec

Companion to: `spec/implementation.md`
Status: ready to implement alongside v0.2 soak

## 1. Goal

Establish baseline measurements for quickjs-wasm's two critical performance axes:

- **Startup time**: how long to go from `import quickjs_wasm` to a working `Context` ready for eval
- **Execution time**: how long JS evaluation takes for representative workloads, measured against the cost of the same logic in pure Python

These numbers inform v0.3's profiling pass, provide regression detection in CI, and give users concrete expectations for agent-interpreter workloads.

## 2. Tooling

**pytest-codspeed** — CodSpeed's pytest plugin. Backward-compatible with pytest-benchmark's `benchmark` fixture API, but adds CodSpeed's wall-time and CPU-simulation instruments for CI regression tracking.

**Local**: `pytest benchmarks/ --codspeed` runs benchmarks with wall-time measurement and prints a results table. No CodSpeed account needed for local runs.

**CI**: `CodSpeedHQ/action@v4` runs benchmarks on push/PR and uploads to CodSpeed for regression detection across commits.

## 3. Directory layout

```
benchmarks/
├── conftest.py              # Shared fixtures (runtime, context, pre-registered host functions)
├── test_startup.py          # Startup/initialization benchmarks
├── test_eval_sync.py        # Sync eval benchmarks
├── test_eval_async.py       # Async eval benchmarks
├── test_marshaling.py       # Value marshaling round-trip benchmarks
├── test_host_functions.py   # Host function call overhead benchmarks
└── README.md                # How to run, what the numbers mean
```

Benchmarks live in `benchmarks/`, not `tests/`. They're not correctness tests — they don't assert behavior, they measure time. `pytest` default collection runs `tests/`; benchmarks run via `pytest benchmarks/ --codspeed` or `make benchmark`.

## 4. Dependencies

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
bench = [
    "pytest-codspeed>=3.0",
]
```

Separate from `dev` extras so CI benchmark runners don't pull test-only deps and vice versa. Developers who want both: `pip install -e ".[dev,bench]"`.

## 5. Benchmark cases

### 5.1 Startup

These measure the fixed cost users pay before any JS runs. For agent workloads that create a fresh context per tool invocation, startup is on the critical path.

| Benchmark | What it measures |
|---|---|
| `bench_runtime_create` | `Runtime()` — wasm module load, instantiation, memory limit configuration |
| `bench_context_create` | `rt.new_context()` — context allocation on an already-loaded runtime |
| `bench_runtime_and_context` | `Runtime()` + `rt.new_context()` — the full cold-start path |
| `bench_context_create_10x` | Creating 10 contexts on one runtime — amortized context cost |

Implementation pattern:

```python
import pytest
from quickjs_wasm import Runtime

def test_runtime_create(benchmark):
    benchmark(Runtime)

def test_context_create(benchmark):
    rt = Runtime()
    def create_ctx():
        ctx = rt.new_context()
        ctx.close()
    benchmark(create_ctx)

def test_runtime_and_context(benchmark):
    def cold_start():
        rt = Runtime()
        ctx = rt.new_context()
        ctx.close()
        rt.close()
    benchmark(cold_start)

def test_context_create_10x(benchmark):
    rt = Runtime()
    def create_10():
        ctxs = [rt.new_context() for _ in range(10)]
        for c in ctxs:
            c.close()
    benchmark(create_10)
```

### 5.2 Sync eval

These measure JS execution overhead for representative workloads, isolating quickjs-wasm's cost from the cost of the JS computation itself.

| Benchmark | What it measures |
|---|---|
| `bench_eval_noop` | `ctx.eval("undefined")` — minimum round-trip through eval pipeline |
| `bench_eval_arithmetic` | `ctx.eval("1 + 2")` — simplest value-producing eval |
| `bench_eval_string_concat` | `ctx.eval("'hello' + ' ' + 'world'")` — string allocation + return |
| `bench_eval_json_parse` | Parse a ~1KB JSON string in JS, return as Python dict |
| `bench_eval_json_parse_10kb` | Parse a ~10KB JSON string — exercises the msgpack scratch buffer |
| `bench_eval_fibonacci_30` | `fib(30)` recursive — pure-compute JS, measures interpreter speed |
| `bench_eval_loop_1m` | `for (let i = 0; i < 1_000_000; i++) {}` — bytecode dispatch overhead |
| `bench_eval_regex` | Regex match on a 1KB string — built-in JS perf |
| `bench_eval_object_create_1k` | Create 1000 objects with 5 properties each — GC pressure |

Implementation uses the `benchmark` fixture so only the `ctx.eval(...)` call is timed, not the setup:

```python
def test_eval_json_parse(benchmark, ctx):
    json_str = '{"key": "value", "nums": [1,2,3], "nested": {"a": true}}'
    code = f"JSON.parse('{json_str}')"
    benchmark(ctx.eval, code)
```

### 5.3 Async eval

Same workloads as sync where applicable, plus async-specific patterns. The goal is to measure the overhead of the async driving loop, the task group, and the event-loop interaction — not the host function's own latency (which is mocked to near-zero).

| Benchmark | What it measures |
|---|---|
| `bench_eval_async_noop` | `await ctx.eval_async("undefined")` — async pipeline minimum |
| `bench_eval_async_immediate_host` | Async host fn that returns immediately (no sleep) — measures pure dispatch + promise-settle overhead |
| `bench_eval_async_fan_out_10` | `Promise.all` with 10 immediate async host calls — concurrent dispatch overhead |
| `bench_eval_async_sequential_10` | 10 sequential `await` calls to immediate async host fns — driving loop iteration cost |

The async benchmarks need a running event loop. pytest-asyncio + pytest-codspeed work together:

```python
import pytest
import asyncio
from quickjs_wasm import Runtime

@pytest.fixture
def async_ctx():
    rt = Runtime()
    ctx = rt.new_context()
    @ctx.function
    async def instant(x):
        return x
    yield ctx
    ctx.close()
    rt.close()

@pytest.mark.benchmark
async def test_eval_async_immediate_host(async_ctx):
    await async_ctx.eval_async("await instant(42)")
```

### 5.4 Marshaling

Isolate the cost of Python ↔ JS value conversion from eval overhead.

| Benchmark | What it measures |
|---|---|
| `bench_marshal_int` | Set and read a single integer via globals |
| `bench_marshal_string_1kb` | Round-trip a 1KB string |
| `bench_marshal_string_100kb` | Round-trip a 100KB string — scratch buffer growth |
| `bench_marshal_dict_flat_100` | Round-trip a flat dict with 100 string keys |
| `bench_marshal_dict_nested_5` | Round-trip a 5-level nested dict |
| `bench_marshal_list_10k_ints` | Round-trip a list of 10,000 integers |
| `bench_marshal_bytes_1mb` | Round-trip 1MB of bytes (Uint8Array path) |

Implementation pattern — use globals write + eval read to measure the full round-trip:

```python
def test_marshal_dict_flat_100(benchmark, ctx):
    data = {f"key_{i}": f"value_{i}" for i in range(100)}
    def round_trip():
        ctx.globals["d"] = data
        return ctx.eval("d")
    benchmark(round_trip)
```

### 5.5 Host function call overhead

Measure the per-call cost of crossing the JS → Python → JS boundary.

| Benchmark | What it measures |
|---|---|
| `bench_host_call_noop` | Call a host fn that returns None — pure dispatch overhead |
| `bench_host_call_identity_int` | Host fn returns its integer argument — minimal marshal both ways |
| `bench_host_call_identity_dict` | Host fn returns a small dict — structured marshal cost |
| `bench_host_call_100x_loop` | JS loop calling a host fn 100 times — amortized per-call cost |
| `bench_host_call_async_noop` | Async host fn that returns immediately — async dispatch overhead vs sync |

```python
def test_host_call_100x_loop(benchmark, ctx):
    @ctx.function
    def inc(n):
        return n + 1

    code = """
        let x = 0;
        for (let i = 0; i < 100; i++) x = inc(x);
        x
    """
    benchmark(ctx.eval, code)
```

## 6. CI workflow

`.github/workflows/benchmarks.yml`:

```yaml
name: Benchmarks
on:
  push:
    branches: ["main"]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read
  id-token: write

jobs:
  benchmarks:
    name: Run benchmarks
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          submodules: true

      - uses: actions/setup-python@v6
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -e ".[dev,bench]"

      - name: Download prebuilt wasm
        uses: actions/download-artifact@v4
        with:
          name: quickjs-wasm
          path: quickjs_wasm/_resources/

      - name: Run benchmarks
        uses: CodSpeedHQ/action@v4
        with:
          mode: walltime
          token: ${{ secrets.CODSPEED_TOKEN }}
          run: pytest benchmarks/ --codspeed
```

**Mode**: `walltime` not `simulation`. Simulation (Valgrind-based) doesn't see wasm execution cost — it profiles the Python host and wasmtime's internal dispatch, not QuickJS's bytecode interpreter inside the wasm module. Wall-time captures the full stack including wasm execution, which is what users actually experience. The trade-off is higher variance on shared CI runners; CodSpeed's macro runners (bare-metal, isolated) eliminate this if variance becomes a problem.

**Wasm artifact**: benchmarks depend on a prebuilt `quickjs.wasm`. The workflow downloads it from the `build-wasm` workflow's artifact. This avoids rebuilding wasm on every benchmark run and ensures benchmarks run against the same binary the tests use.

## 7. Local usage

```bash
# Install bench extras
pip install -e ".[dev,bench]"

# Run all benchmarks locally
pytest benchmarks/ --codspeed

# Run a specific benchmark file
pytest benchmarks/test_startup.py --codspeed

# Run without codspeed (just as regular tests, no timing)
pytest benchmarks/

# Quick comparison: run twice and diff
pytest benchmarks/ --codspeed --codspeed-output=before.json
# make a change
pytest benchmarks/ --codspeed --codspeed-output=after.json
```

Add to `Makefile` (or `pyproject.toml` scripts):

```makefile
.PHONY: benchmark
benchmark:
	pytest benchmarks/ --codspeed
```

## 8. What the numbers should look like (order-of-magnitude targets)

These are rough expectations for v0.2 on a modern laptop (M-series Mac or recent x86), not hard SLAs. They exist so a developer reading benchmark output for the first time can tell "that looks right" vs "something is broken."

| Category | Benchmark | Expected range |
|---|---|---|
| Startup | `bench_runtime_create` | 5–20 ms |
| Startup | `bench_context_create` | 0.1–1 ms |
| Startup | `bench_runtime_and_context` | 5–25 ms |
| Eval | `bench_eval_noop` | 10–50 µs |
| Eval | `bench_eval_arithmetic` | 10–50 µs |
| Eval | `bench_eval_fibonacci_30` | 5–20 ms |
| Eval | `bench_eval_loop_1m` | 20–80 ms |
| Marshal | `bench_marshal_dict_flat_100` | 100–500 µs |
| Marshal | `bench_marshal_bytes_1mb` | 1–5 ms |
| Host call | `bench_host_call_noop` | 20–100 µs |
| Host call | `bench_host_call_100x_loop` | 2–10 ms |
| Async | `bench_eval_async_noop` | 50–200 µs |
| Async | `bench_eval_async_fan_out_10` | 200–1000 µs |

If a benchmark lands outside its range, that's worth investigating — either the range is wrong (update it) or the implementation has an unexpected cost center.

## 9. Rules for benchmark code

- **No assertions in benchmarks.** Benchmarks measure, they don't verify. Correctness belongs in `tests/`.
- **Use the `benchmark` fixture for fine-grained control.** `@pytest.mark.benchmark` measures the whole test function including setup; the `benchmark(fn, *args)` fixture measures only the call.
- **Setup outside the measured region.** Create runtimes, contexts, register host functions, prepare data structures in fixture or before the `benchmark()` call. Only the operation under test goes inside `benchmark()`.
- **One operation per benchmark.** Don't combine "create runtime + eval" into one benchmark unless you're explicitly measuring cold-start-to-first-eval. Split so regressions are attributable.
- **Stable inputs.** Use deterministic, fixed-size inputs. No random data, no network, no filesystem. Benchmarks must be reproducible.
- **Name benchmarks by what they measure, not what they test.** `bench_eval_fibonacci_30` not `test_fib_perf`. The `bench_` prefix visually distinguishes from `test_` correctness tests.
