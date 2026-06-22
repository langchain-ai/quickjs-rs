# quickjs-rs

Sandboxed JavaScript execution for Python.

JS runs inside a WebAssembly sandbox: [quickjs-ng](https://quickjs-ng.github.io/quickjs/) (a QuickJS fork) via [rquickjs](https://github.com/DelSkayn/rquickjs) is compiled to `wasm32-wasip1` and driven by [wasmtime](https://wasmtime.dev/). The package is pure Python - one universal wheel that bundles the guest `.wasm` artifacts; the only runtime dependency is `wasmtime`. ES modules resolve through a host loader callback; inline TypeScript is type-stripped by a separate OXC-backed transform `.wasm` before QuickJS sees it.

> [!WARNING]
> `quickjs-rs` is experimental. Before putting this in production, you should read the [Security](#security) guide.

## Install

```bash
pip install quickjs-rs
uv add quickjs-rs
```

Ships as a single universal pure-Python wheel (`py3-none-any`) — the bundled
guest is platform-independent WebAssembly, and `wasmtime` supplies the
per-platform runtime. Requires Python 3.11+; runs anywhere `wasmtime` has a
wheel (Linux, macOS, Windows; x86_64 + arm64).

## Quickstart

```python
from quickjs_rs import Runtime

with Runtime() as rt:
    with rt.new_context() as ctx:
        assert ctx.eval("1 + 2") == 3

        # Register a Python callable as a JS global.
        @ctx.function
        def greet(name: str) -> str:
            return f"hi {name}"
        assert ctx.eval("greet('world')") == "hi world"
```

Async + top-level `await`:

```python
import asyncio

async def main():
    with Runtime() as rt:
        with rt.new_context() as ctx:
            @ctx.function
            async def fetch_thing() -> str:
                await asyncio.sleep(0.01)
                return "from python"

            result = await ctx.eval_async("await fetch_thing()")
            assert result == "from python"

asyncio.run(main())
```

## ES modules

Supply modules through a host loader callback pair: `normalize(base, specifier)` resolves an import to a canonical name, and
`load(name)` returns the source. The host owns all resolution policy — there is
no built-in scope model.

```python
import posixpath
from quickjs_rs import Runtime

sources = {
    "@agent/config": "export const MAX_RETRIES = 3;",
    "@agent/utils": "export { slugify } from './strings.js';",
    "@agent/utils/strings.js":
        "export const slugify = s => s.toLowerCase().replace(/ /g, '-');",
}

def normalize(base, spec):
    if not spec.startswith("."):
        return spec                       # bare name → canonical name
    base_dir = base if "." not in posixpath.basename(base) else posixpath.dirname(base)
    return posixpath.normpath(posixpath.join(base_dir, spec))  # relative → joined

with Runtime() as rt:
    rt.set_module_loader(normalize=normalize, load=sources.get)
    with rt.new_context() as ctx:
        assert await ctx.eval_async("""
            const { slugify } = await import("@agent/utils");
            const { MAX_RETRIES } = await import("@agent/config");
            slugify("Hello World") + '/' + MAX_RETRIES;
        """) == "hello-world/3"
```

`normalize` is where sandboxing lives — return `None` to refuse a specifier.

## TypeScript

Module sources whose canonical name ends in `.ts`, `.mts`, `.cts`, or `.tsx` are
type-stripped by the host transform adapter before evaluation. Enums,
namespaces, and parameter properties are transformed; plain type annotations
erase. No type checking — run `tsc --noEmit` separately for that.

```python
# A canonical name ending in .ts/.tsx is stripped before QuickJS sees it.
ts_sources = {
    "util.ts": """
        export enum Mode { Strict = 1, Loose = 2 }
        export function slug(s: string, mode: Mode): string {
            return s.toLowerCase().replace(/ /g, mode === Mode.Strict ? '_' : '-');
        }
    """,
}

with Runtime() as rt:
    rt.set_module_loader(load=ts_sources.get)
    with rt.new_context() as ctx:
        assert await ctx.eval_async(
            "const { slug, Mode } = await import('util.ts');"
            "slug('Hello World', Mode.Strict)"
        ) == "hello_world"
```

A TypeScript parse error surfaces as a module-load error rather than at eval.

Transform flags are also public for hosts that need a different policy. Runtime
flags apply to top-level eval calls and module-loaded sources:

```python
from quickjs_rs import Runtime, SourceTransform

with Runtime(transform_flags=SourceTransform.TOP_LEVEL_CONST_TO_VAR) as rt:
    with rt.new_context() as ctx:
        ctx.eval("const exposed = 1;")
        assert ctx.eval("globalThis.exposed") == 1
```

For TypeScript sources, choose the source kind explicitly:

```python
from quickjs_rs import Runtime, SourceTransform

with Runtime(transform_flags=SourceTransform.SOURCE_TS | SourceTransform.STRIP_TYPESCRIPT) as rt:
    with rt.new_context() as ctx:
        assert ctx.eval("const value: number = 2; value") == 2
```

Eval calls can override the runtime policy:

```python
ctx.eval("const scoped = 1;", transform_flags=SourceTransform.NONE)
```

Module loaders can also override the runtime policy per canonical module name.
For example, this enables the extra top-level `const` to `var` rewrite while
keeping the default TypeScript/TSX module behavior:

```python
from quickjs_rs import SourceTransform, default_module_transform_flags

def transform_flags(name):
    return default_module_transform_flags(name) | SourceTransform.TOP_LEVEL_CONST_TO_VAR

rt.set_module_loader(load=sources.get, transform_flags=transform_flags)
```

Policy precedence is: per-eval `transform_flags`, then module-loader
`transform_flags`, then runtime `transform_flags`, then the default module
TypeScript/TSX policy. Pass `SourceTransform.NONE` to disable transforms
explicitly.

For one-off transforms outside module loading, use `transform_source()`:

```python
from quickjs_rs import SourceTransform, transform_source

js = transform_source(
    "plain.js",
    "export const value = 1;",
    flags=SourceTransform.TOP_LEVEL_CONST_TO_VAR,
)
```

## Snapshots

A snapshot captures the **entire** guest heap — every object, the atom table,
the job queue, closures, and pending promises — as a flat image, and
reconstitutes it into a fresh context. Because it's the whole VM memory,
aliasing and closure state survive exactly.

```python
from quickjs_rs import Runtime, Snapshot

with Runtime() as rt:
    with rt.new_context() as ctx:
        ctx.eval("""
            const shared = { count: 1 };
            const a = shared, b = shared;
            const counter = (() => { let n = 0; return () => ++n; })();
        """)
        payload = ctx.create_snapshot().to_bytes()

with Runtime() as rt2:
    with rt2.new_context() as ctx2:
        rt2.restore_snapshot(Snapshot.from_bytes(payload), ctx2)
        assert ctx2.eval("a === b") is True       # aliasing preserved
        assert ctx2.eval("counter()") == 1        # closure state preserved
```

A snapshot must be taken at a quiescent point (no in-flight `eval_async`, no
pending async host calls); `create_snapshot_async()` is the async-context form.
Restore validates a fail-closed header — including a `build_id` that **rejects a
snapshot taken from a different guest build** — before writing the image. Treat
snapshot bytes as trusted input (see [Security](#security)).

```python
snap = await ctx.create_snapshot_async()
rt.restore_snapshot(snap, other_ctx, inject_globals=True)
```

## Security

- **The WebAssembly sandbox is the isolation boundary.** JS executes inside the
  guest's wasm linear memory; quickjs-ng never sees a host pointer and cannot
  read or write Python's address space. A bug in the JS engine is contained
  within the sandbox, not a path to host-memory compromise — this is the
  central reason JS runs in wasm rather than as a native extension.

- **The residual runtime-escape risk is wasmtime itself.** wasmtime executes the
  guest in-process, so a vulnerability in wasmtime / Cranelift (the JIT) is the
  one path that could cross the sandbox boundary. Keep `wasmtime` updated. For
  hostile multi-tenant workloads where you must defend against an active
  runtime-attacker, add process/container isolation on top and recycle on
  timeout/OOM — the sandbox raises the bar but does not replace defense in depth.

- **Registered host callbacks are capability boundaries.** Anything you expose to
  JS via `ctx.register(...)` is reachable by the sandboxed code; treat every such
  callback as privileged when running untrusted JS. The sandbox contains the
  engine, not the capabilities you hand it.

- **Resource limits are enforced** — `Runtime(memory_limit=...)` caps heap, a
  per-eval timeout interrupts runaway JS (the instance survives), and a runaway
  recursion is contained by the sandbox (it traps the wasm instance rather than
  the host; see the [threat model](.github/THREAT_MODEL.md) for the wasi
  stack-check caveat).

- **Each `Context` is its own isolated wasm instance** — separate linear memory,
  no shared globals/modules. Still, use one `Runtime` per trust domain.

- **Snapshots are trusted input.** A whole-memory snapshot is an arbitrary guest
  heap image. Restore validates a fail-closed header (incl. a `build_id` that
  rejects a snapshot taken from a different guest build), but a same-build
  *crafted* image is not made safe by that check — do not restore snapshots from
  an untrusted source, and do not restore across guest builds.


## Development

```bash
# Build the wasm guests (needs the Rust toolchain + the wasm target):
#   rustup target add wasm32-wasip1
python scripts/build_guest.py        # cargo build -> quickjs_rs/_guest.wasm + _transform.wasm

# Dev install (pure-Python package; wasm is bundled above).
pip install -e ".[dev]"

# Run tests, type-check, lint.
pytest
mypy quickjs_rs
ruff check
```

## License

MIT. See [`LICENSE`](LICENSE).
