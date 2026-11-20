# quickjs-rs

[![PyPI](https://img.shields.io/pypi/v/quickjs-rs)](https://pypi.org/project/quickjs-rs/)
[![Python versions](https://img.shields.io/pypi/pyversions/quickjs-rs)](https://pypi.org/project/quickjs-rs/)
[![License](https://img.shields.io/pypi/l/quickjs-rs)](https://github.com/langchain-ai/quickjs-rs/blob/main/LICENSE)
[![test](https://github.com/langchain-ai/quickjs-rs/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/langchain-ai/quickjs-rs/actions/workflows/test.yml)

quickjs-rs runs JavaScript from Python, inside a sandbox, in the same process.

It exists for one job: a language model writes a short script that calls functions you provide, and you run it.
The script is untrusted, the functions it calls may take seconds, and it has to stop if it loops forever or allocates without end.
A container per script is slow and heavy for that.
quickjs-rs starts a fresh JavaScript VM in a couple of milliseconds, after a one-time compile when the process
creates its first one. The VM runs under wasmtime, with no access to anything you did not hand it.

```bash
pip install quickjs-rs
# or
uv add quickjs-rs
```

```python
from quickjs_rs import Runtime

with Runtime() as rt, rt.new_context() as ctx:

    @ctx.function
    def get_price(sku: str) -> float:
        return {"A1": 3.5, "B2": 12.0}[sku]

    total = ctx.eval("""
        const skus = ["A1", "B2", "B2"];
        skus.map((sku) => get_price(sku)).reduce((a, b) => a + b, 0)
    """)
    print(total)
    #> 27.5
```

`get_price` is the only thing in the script's world that is not plain JavaScript.
There is no file system, no network, no `console`, and no `fetch`, because the VM has no way to express them.

## Why quickjs-rs

- **A real sandbox in your process.** The engine, [quickjs-ng](https://quickjs-ng.github.io/quickjs/), is compiled
  to WebAssembly, and each context runs in its own wasm instance. WebAssembly's sandboxing keeps every context
  walled off from your Python process and from every other context.
- **Host functions are plain Python functions.** Register a function, sync or `async`, and scripts call it like any
  other JavaScript function. Async host functions return promises.
- **Resource limits.** Each context has a memory limit (64 MiB by default) and each eval a timeout (5 seconds by
  default).
- **TypeScript without a build step.** Modules named `.ts` or `.tsx` have their types stripped on the way in.
- **Snapshots.** Copy a context's whole state to bytes and restore it later, closures and pending promises included.
- **Easy to ship.** One pure-Python wheel that carries the compiled `.wasm`. The only runtime dependency is
  [`wasmtime`](https://pypi.org/project/wasmtime/), so it installs anywhere wasmtime does.

## What it is not

- **Not Node or a browser.** There is no `require`, no timers, and no web APIs. Anything a script needs from the
  outside, you provide as a host function.
- **Not an OS sandbox.** wasmtime runs the guest inside your Python process. For workloads that must assume an
  attacker who can exploit wasmtime itself, run quickjs-rs inside a container or a separate process as well.
- **Not a type checker.** TypeScript types are erased, not checked.

## Contents

- [Get started](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/get-started.md): Install quickjs-rs, evaluate a script, give it a host function, and run async code.

**Concepts**

- [Host functions](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/concepts/host-functions.md): Expose Python functions to JavaScript, sync or async, and control what scripts see when they fail.
- [Values](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/concepts/values.md): How values convert between Python and JavaScript, and how to keep a value in the sandbox with a Handle.
- [Async evaluation](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/concepts/async.md): Run scripts that use await and async host functions, and how quickjs-rs counts time while they wait.
- [Modules and TypeScript](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/concepts/modules.md): Load ES modules through a host loader you control, and run TypeScript with types stripped.
- [Resource limits](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/concepts/resource-limits.md): Cap memory and running time for each script, control the clock it sees, and know what the limits do not cover.
- [Snapshots](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/concepts/snapshots.md): Save a context's whole JavaScript state as bytes and restore it into another context or process.
- [Threads](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/concepts/threads.md): Which objects can be shared across threads, and how ThreadWorker keeps scripts off your event loop.
- [Security model](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/concepts/security-model.md): What the quickjs-rs sandbox protects, what it leaves to you, and how to run untrusted code with it.

**Reference**

- [Architecture](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/reference/architecture.md): The wasmtime objects behind each context, what is shared and what is not, and how values, host calls, and evals cross the boundary.
- [Engine ABI](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/reference/abi.md): The functions, memory rules, and handle ownership between the Python host and the wasm engine.
- [Threat model](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/reference/threat-model.md): Assets, attackers, trust boundaries, and threats for quickjs-rs, with what the library does about each and what is left to you.
- [Python API](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/reference/api.md): Every public class and function in quickjs_rs, with signatures and behavior.
- [Errors](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/reference/errors.md): Every exception quickjs-rs raises, when each one happens, and how JavaScript errors map to Python classes.
- [Limitations](https://github.com/langchain-ai/quickjs-rs/blob/main/docs/reference/limitations.md): Known gaps in quickjs-rs, what happens today, and what to do about each one.

## Development

Building the wasm guests needs Rust with the `wasm32-wasip1` target (`rustup target add wasm32-wasip1`).

```bash
python scripts/build_guest.py     # builds quickjs_rs/_guest.wasm and _transform.wasm
pip install -e ".[dev]"
pytest
mypy quickjs_rs
ruff check
```

`tests/test_docs.py` runs every Python example in this file and in `docs/`, and checks the links between pages.

## License

MIT. See [`LICENSE`](https://github.com/langchain-ai/quickjs-rs/blob/main/LICENSE).
