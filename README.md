# quickjs-rs

Sandboxed JavaScript execution for Python.

Native Python extension (PyO3 + [rquickjs](https://github.com/DelSkayn/rquickjs)) wrapping [quickjs-ng](https://quickjs-ng.github.io/quickjs/) (a QuickJS fork). Single self-contained wheel, zero runtime dependencies, microsecond-range runtime startup. ES modules with a composable scope registry. Inline TypeScript support via [oxidase](https://github.com/branchseer/oxidase).

> [!WARNING]
> `quickjs-rs` is experimental. Before putting this in production, you should read the [Security](#security) guide.

## Install

```bash
pip install quickjs-rs
uv add quickjs-rs
```

Wheels ship for Linux (x86_64 + aarch64), macOS (x86_64 + arm64), and Windows (x86_64), against Python 3.11, 3.12, and 3.13.

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

Register modules via `ModuleScope`, then `import` them from module-mode eval. Scopes are recursive, self-contained resolver boundaries — each scope sees only what its own dict declares.

```python
from quickjs_rs import ModuleScope, Runtime

stdlib = ModuleScope({
    "@agent/utils": ModuleScope({
        "index.js": """
            export { slugify } from './strings.js';
        """,
        "strings.js": """
            export function slugify(s) {
                return s.toLowerCase().replace(/ /g, '-');
            }
        """,
    }),
    "@agent/config": ModuleScope({
        "index.js": "export const MAX_RETRIES = 3;",
    }),
})

with Runtime() as rt:
    with rt.new_context() as ctx:
        ctx.install(stdlib)
        assert await ctx.eval_async("""
            const { slugify } = await import("@agent/utils");
            const { MAX_RETRIES } = await import("@agent/config");
            slugify("Hello World") + '/' + MAX_RETRIES;
        """) == "hello-world/3"
```

Shared deps are declared by spreading (`**utils.modules`) into each scope that needs them. See [`spec/module-loading.md`](spec/module-loading.md) for the full resolver rules and patterns.

## TypeScript

Source strings whose key ends in `.ts`, `.mts`, `.cts`, or `.tsx` are type-stripped at `install()` time via oxidase. Enums, namespaces, and parameter properties are transformed; plain type annotations erase to whitespace. No type checking — run `tsc --noEmit` separately if you want that.

```python
ctx.install(ModuleScope({
    "@util": ModuleScope({
        "index.ts": """
            export enum Mode { Strict = 1, Loose = 2 }
            export function slug(s: string, mode: Mode): string {
                return s.toLowerCase().replace(/ /g, mode === Mode.Strict ? '_' : '-');
            }
        """,
    }),
}))
```

TypeScript syntax errors surface at `install()` time (oxidase parses during stripping) rather than at eval.

## Security

- This library is not a host-memory isolation boundary. The JS engine (`quickjs-ng` via `rquickjs`/`rquickjs-sys`) runs in the same process/address space as Python.

  - When running untrusted or semi-trusted JS, run execution in isolated worker processes/containers with restricted network/filesystem access and recycle workers on timeout/OOM/failure.

- Registered host callbacks are capability boundaries. Any callback exposed to JS should be treated as privileged if this runtime is being used to run untrusted code

- Do not share a single `Runtime` across different trust domains/tenants. Use one runtime per trust domain to avoid cross-context module contamination.

See [`.github/THREAT_MODEL.md`](.github/THREAT_MODEL.md) for more information on the threat boundaries and supply-chain posture of `quickjs-rs`


## Development

```bash
# Dev install (maturin handles the Rust build).
pip install -e ".[dev]"
maturin develop --release

# Run tests, type-check, lint.
pytest
mypy quickjs_rs
ruff check
```

## License

MIT. See [`LICENSE`](LICENSE).
