# Modules and TypeScript

The sandbox has no filesystem, so `import` cannot find anything on its own. You install a loader: two Python callbacks
that turn an import specifier into a module name, and a module name into source code. The loader is where you decide
what a script may import.

## The basics

```python
import asyncio

from quickjs_rs import Runtime

sources = {
    "@agent/config": "export const MAX_RETRIES = 3;",
    "@agent/strings": "export const slugify = (s) => s.toLowerCase().replace(/ /g, '-');",
}


async def main() -> None:
    with Runtime() as rt:
        rt.set_module_loader(load=sources.get)
        with rt.new_context() as ctx:
            print(await ctx.eval_async("""
                const { slugify } = await import("@agent/strings");
                const { MAX_RETRIES } = await import("@agent/config");
                `${slugify("Hello World")} x${MAX_RETRIES}`
            """))
            #> hello-world x3


asyncio.run(main())
```

`load(name)` returns the source for a module name, or `None` if there is no such module. The import then fails inside
the script:

```python
import asyncio

from quickjs_rs import Runtime


async def main() -> None:
    with Runtime() as rt:
        rt.set_module_loader(load={"allowed": "export default 1;"}.get)
        with rt.new_context() as ctx:
            print(await ctx.eval_async("""
                try { await import("node:fs") } catch (e) { e.name }
            """))
            #> ReferenceError


asyncio.run(main())
```

If `load` raises, the script sees the same failure. Your exception does not reach it.

## Resolve specifiers with `normalize`

Without `normalize`, the specifier is used as the module name as written. Pass `normalize(base, specifier)` to resolve
relative imports or to apply rules. `base` is the name of the importing module. Return the resolved name, or `None` to
refuse.

```python
import asyncio
import posixpath

from quickjs_rs import Runtime

sources = {
    "lib/index.js": "export { slugify } from './strings.js';",
    "lib/strings.js": "export const slugify = (s) => s.toLowerCase().replace(/ /g, '-');",
}


def normalize(base: str, specifier: str) -> str | None:
    if specifier.startswith("."):
        name = posixpath.normpath(posixpath.join(posixpath.dirname(base), specifier))
    else:
        name = specifier
    return name if name in sources else None


async def main() -> None:
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=sources.get)
        with rt.new_context() as ctx:
            print(await ctx.eval_async("""
                const { slugify } = await import("lib/index.js");
                slugify("Hello World")
            """))
            #> hello-world


asyncio.run(main())
```

Both callbacks receive names the script chose, including `../` paths. If your loader reads files or fetches URLs,
check every name against an allowlist.

Some details:

- Both callbacks are synchronous and run while the script waits. The timeout cannot interrupt them.
- Names longer than 4096 bytes and sources larger than 16 MiB count as not found.
- The loader belongs to the runtime and is shared by all its contexts.

## Modules load once per context

Each context caches every module it loads, and every `normalize` answer, for its whole life. Importing a module a
second time returns the same instance, even if `load` would now return different source. Use a new context to pick up
changes.

## Run a module

Dynamic `import()` works in any eval. To run code that uses `import` and `export` statements itself, pass
`module=True`. The call returns `None`, so publish results through a global:

```python
import asyncio

from quickjs_rs import Runtime


async def main() -> None:
    with Runtime() as rt:
        rt.set_module_loader(load={"math": "export const double = (n) => n * 2;"}.get)
        with rt.new_context() as ctx:
            await ctx.eval_async(
                "import { double } from 'math'; globalThis.answer = double(21);",
                module=True,
            )
            print(ctx.eval("answer"))
            #> 42


asyncio.run(main())
```

A context that has run `module=True` code cannot be [snapshotted](../concepts/snapshots.md).

## TypeScript

Modules whose name ends in `.ts`, `.mts`, or `.cts` are stripped as TypeScript before they run, and `.tsx` as TSX.
Types are erased, and `enum`, `namespace`, and parameter properties become plain JavaScript. Nothing is type-checked.

```python
import asyncio

from quickjs_rs import Runtime

sources = {
    "util.ts": """
        export enum Sep { Dash = "-", Underscore = "_" }
        export function slug(s: string, sep: Sep = Sep.Dash): string {
            return s.toLowerCase().replace(/ /g, sep);
        }
    """,
}


async def main() -> None:
    with Runtime() as rt:
        rt.set_module_loader(load=sources.get)
        with rt.new_context() as ctx:
            print(await ctx.eval_async("""
                const { slug, Sep } = await import("util.ts");
                slug("Hello World", Sep.Underscore)
            """))
            #> hello_world


asyncio.run(main())
```

Code passed to `eval` is plain JavaScript by default. To write TypeScript there, pass `transform_flags`:

```python
from quickjs_rs import Runtime, SourceTransform

TS = SourceTransform.SOURCE_TS | SourceTransform.STRIP_TYPESCRIPT

with Runtime() as rt, rt.new_context() as ctx:
    print(ctx.eval("const n: number = 41; n + 1", transform_flags=TS))
    #> 42
```

A TypeScript syntax error raises `JSError` with the name `SyntaxError` before any code runs.

### Transform flags

| Flag | Effect |
| --- | --- |
| `SOURCE_TS` | Parse the source as TypeScript. |
| `SOURCE_TSX` | Parse the source as TSX. |
| `STRIP_TYPESCRIPT` | Erase types and lower TypeScript-only syntax. |
| `TOP_LEVEL_CONST_TO_VAR` | Rewrite top-level `const` to `var`, so the binding becomes a property of `globalThis`. |
| `NONE` | No transform. |

quickjs-rs uses the first of these that is set:

1. `transform_flags=` on the eval call, for that code only.
2. `transform_flags=` on `set_module_loader`, for loaded modules.
3. `Runtime(transform_flags=...)`.
4. The default: modules by file extension, eval code untouched.

Each can be a fixed `SourceTransform` or a function from module name to `SourceTransform`.
`default_module_transform_flags(name)` returns the default for a name, so you can add to it.

The transformer is [OXC](https://oxc.rs) compiled to its own WebAssembly module. Each context gets its own instance
of it, created the first time it is needed.

## See also

- [Security model](../concepts/security-model.md): why the loader is part of your attack surface.
- [Async evaluation](../concepts/async.md): `eval_async`, which dynamic `import()` needs.
