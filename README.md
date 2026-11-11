# quickjs-rs

Sandboxed JavaScript execution for Python.

[quickjs-ng](https://quickjs-ng.github.io/quickjs/) (via [rquickjs](https://github.com/DelSkayn/rquickjs)) is compiled
to `wasm32-wasip1` and run by [wasmtime](https://wasmtime.dev/). Every context is its own WebAssembly instance, so
scripts can't touch Python's memory, and they reach the outside world only through functions you register. Inline
TypeScript is type-stripped by a separate OXC-based wasm module. The package is one pure-Python wheel; its only
runtime dependency is `wasmtime`.

**Documentation: https://langchain-ai.github.io/quickjs-rs/** (source in [`docs/`](docs/)): usage guide,
architecture, [security model](https://langchain-ai.github.io/quickjs-rs/security) and
[threat model](https://langchain-ai.github.io/quickjs-rs/security/threat-model).

> [!WARNING]
> `quickjs-rs` is experimental. Read the [security model](https://langchain-ai.github.io/quickjs-rs/security)
> before running untrusted code with it.

## Install

```bash
pip install quickjs-rs
uv add quickjs-rs
```

Python 3.11+, anywhere `wasmtime` has a wheel (Linux, macOS, Windows; x86_64 and arm64).

## Quickstart

```python
from quickjs_rs import Runtime

with Runtime() as rt:
    with rt.new_context() as ctx:
        assert ctx.eval("[1, 2, 3].map(x => x * 2)") == [2, 4, 6]

        @ctx.function
        def lookup(user_id: int) -> dict:
            return {"id": user_id, "name": "Ada"}

        assert ctx.eval("lookup(7).name") == "Ada"
```

Async host functions and top-level `await`:

```python
import asyncio
from quickjs_rs import Runtime

async def main() -> None:
    with Runtime() as rt:
        with rt.new_context(timeout=2.0) as ctx:
            @ctx.function
            async def search(q: str) -> list[str]:
                await asyncio.sleep(0.1)
                return [f"{q}-1", f"{q}-2"]

            hits = await ctx.eval_async("""
                const [a, b] = await Promise.all([search("x"), search("y")]);
                [...a, ...b].length
            """)
            assert hits == 4

asyncio.run(main())
```

## Features

- **Limits.** `Runtime(memory_limit=...)` caps QuickJS allocations per context (default 64 MiB) and
  `new_context(timeout=...)` sets a wall-clock budget per eval (default 5 s). Both raise (`MemoryLimitError`,
  `TimeoutError`) and leave the context usable. [Limits](https://langchain-ai.github.io/quickjs-rs/guide/limits)
  covers what they don't bound.
- **ES modules** through a host loader: `rt.set_module_loader(normalize=..., load=...)`. The host owns all resolution
  policy. [Modules](https://langchain-ai.github.io/quickjs-rs/guide/modules-and-typescript)
- **TypeScript.** Modules named `.ts`/`.mts`/`.cts`/`.tsx` are type-stripped before QuickJS sees them; evals can opt in
  with `transform_flags`. No type checking.
- **Snapshots.** `ctx.create_snapshot()` copies the whole guest memory: closures, aliasing and the job queue survive a
  restore into another context. [Snapshots](https://langchain-ai.github.io/quickjs-rs/guide/snapshots)
- **Host clock.** `Runtime(clock=lambda: ns)` backs every guest clock, which also fixes `Math.random()`'s seed.

```python
import asyncio
from quickjs_rs import Runtime, Snapshot

sources = {"@agent/util.ts": "export const slug = (s: string): string => s.toLowerCase().replace(/ /g, '-');"}

async def main() -> None:
    with Runtime() as rt:
        rt.set_module_loader(load=sources.get)
        with rt.new_context() as ctx:
            assert await ctx.eval_async(
                "const { slug } = await import('@agent/util.ts'); slug('Hello World')"
            ) == "hello-world"

asyncio.run(main())

with Runtime() as rt:
    with rt.new_context() as ctx:
        ctx.eval("const counter = (() => { let n = 0; return () => ++n; })(); counter();")
        payload = ctx.create_snapshot().to_bytes()
    with rt.new_context() as ctx2:
        rt.restore_snapshot(Snapshot.from_bytes(payload), ctx2)
        assert ctx2.eval("counter()") == 2
```

## Security in brief

- **The wasm sandbox is the boundary.** A bug in QuickJS stays inside the guest's linear memory. The remaining escape
  path is a wasmtime or Cranelift bug: keep `wasmtime` current, and add process or container isolation for hostile
  multi-tenant workloads.
- **Host functions are capabilities.** Untrusted code can call everything you register, with any arguments. Validate
  inputs and budget calls inside them.
- **Limits have gaps.** The timeout doesn't count time spent awaiting host calls and can't interrupt a blocking sync
  host function. Deep recursion traps the instance (`wasmtime.Trap`) instead of raising a `QuickJSError`; discard the
  context. On 0.2.6 and earlier, `memory_limit` undercounts allocations badly; later releases count real sizes.
- **Snapshots are trusted input.** Restore checks the image came from the same guest build, but images aren't signed:
  a crafted image is arbitrary guest state. Store them with integrity protection and don't restore untrusted ones.
- **Use one `Runtime` per tenant and per thread.** Contexts never share memory, but a runtime shares its loader, clock
  and timeout slot.

Details: [security model](https://langchain-ai.github.io/quickjs-rs/security),
[threat model](https://langchain-ai.github.io/quickjs-rs/security/threat-model).

## Development

```bash
# Build the wasm guests (needs Rust + `rustup target add wasm32-wasip1`):
python scripts/build_guest.py        # -> quickjs_rs/_guest.wasm + _transform.wasm

pip install -e ".[dev]"
pytest
mypy quickjs_rs
ruff check

# Docs site (Node 26+, pnpm via corepack or pnpm/action-setup):
cd docs && pnpm install && pnpm dev
```

## License

MIT. See [`LICENSE`](LICENSE).
