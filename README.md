# quickjs-rs

Sandboxed JavaScript execution for Python.

Native Python extension (PyO3 + [rquickjs](https://github.com/DelSkayn/rquickjs)) wrapping [quickjs-ng](https://quickjs-ng.github.io/quickjs/) (a QuickJS fork). Single self-contained wheel, zero runtime dependencies, microsecond-range runtime startup. ES modules are loaded through a host import handler.

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

Register a runtime import handler, then `import` modules from module-mode eval. The handler receives `(requested_key, referrer, specifier)` and returns source text or `None`; `referrer` is the importing module key, or `None` for top-level eval.

```python
from quickjs_rs import Runtime

sources = {
    "@agent/utils": """
        export function slugify(s) {
            return s.toLowerCase().replace(/ /g, '-');
        }
    """,
    "@agent/config": "export const MAX_RETRIES = 3;",
}

def module_source(requested_key: str, referrer: str | None, specifier: str) -> str | None:
    return sources.get(requested_key)

with Runtime() as rt:
    rt.set_import_handler(module_source)
    with rt.new_context() as ctx:
        assert await ctx.eval_async("""
            const { slugify } = await import("@agent/utils");
            const { MAX_RETRIES } = await import("@agent/config");
            slugify("Hello World") + '/' + MAX_RETRIES;
        """) == "hello-world/3"
```

Bare imports are passed to the handler unchanged. Relative imports are normalized against the importing module key before the handler is called. If you want package-style `index.js` aliases, implement that policy in the handler/backend.

```python
def module_source(requested_key: str, referrer: str | None, specifier: str) -> str | None:
    # requested_key is "@/skills/foo" for import("@/skills/foo").
    return db_lookup(requested_key)  # source text or None

with Runtime() as rt:
    rt.set_import_handler(module_source)
    with rt.new_context() as ctx:
        await ctx.eval_async('const mod = await import("@/skills/foo");', module=True)
```

## TypeScript

Source strings whose requested key ends in `.ts`, `.mts`, `.cts`, or `.tsx` are type-stripped before module load. No type checking: run `tsc --noEmit` separately if you want that.

```python
def module_source(requested_key: str, referrer: str | None, specifier: str) -> str | None:
    if requested_key == "@util/index.ts":
        return """
            export enum Mode { Strict = 1, Loose = 2 }
            export function slug(s: string, mode: Mode): string {
                return s.toLowerCase().replace(/ /g, mode === Mode.Strict ? '_' : '-');
            }
        """
    return None
```

TypeScript syntax errors surface while resolving the import.

## Snapshots

`quickjs-rs` can snapshot the restorable portion of a context's script-mode top-level state and restore it into another context.

It does **not** attempt to snapshot module-local bindings, pending async work, host callback identity, or full lexical-environment state.

```python
from quickjs_rs import Runtime, Snapshot

with Runtime() as rt:
    with rt.new_context() as ctx:
        ctx.eval("""
            const shared = { count: 1 };
            const a = shared;
            const b = shared;
        """)
        snap = ctx.create_snapshot()
        payload = snap.to_bytes()

with Runtime() as rt2:
    with rt2.new_context() as ctx2:
        snap = Snapshot.from_bytes(payload)
        rt2.restore_snapshot(snap, ctx2)
        assert ctx2.eval("a === b") is True
        assert ctx2.eval("a.count") == 1
```

Snapshot creation supports two policy knobs:

- `on_missing_name`: `skip`, `tombstone`, or `error`
- `on_unserializable`: `tombstone` or `error`

Example:

```python
with Runtime() as rt:
    with rt.new_context() as ctx:
        ctx.eval("const fn = () => 1;")
        snap = ctx.create_snapshot(on_unserializable="tombstone")
```

On restore, a tombstoned name is installed as a global property whose getter throws a descriptive error if read. This makes missing or unserializable bindings explicit instead of silently disappearing unless you choose `skip`.

Async contexts use the same snapshot model:

```python
snap = await ctx.create_snapshot_async(on_missing_name="tombstone")
rt.restore_snapshot(snap, other_ctx, inject_globals=True)
```

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
