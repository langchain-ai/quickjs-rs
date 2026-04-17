# quickjs-rs module loading spec

Version: 0.4.0 target
Status: planning
Prerequisite: quickjs-rs v0.3 (PyO3 + rquickjs rewrite, complete)

## 1. Overview

ES module support for quickjs-rs. One new type — `ModuleScope` — provides a recursive, composable module registry that agents import from conventionally. Host functions remain globals registered via `@ctx.function`; JS modules wrap them with clean export surfaces.

The motivating pattern:

```python
stdlib = ModuleScope({
    "@agent/fs": ModuleScope({
        "index.js": """
            export async function readFile(path) { return await _readFile(path); }
            export async function writeFile(path, c) { return await _writeFile(path, c); }
        """,
    }),
    "@agent/utils": ModuleScope({
        "index.js": """
            export { slugify } from "./strings.js";
            export { chunk } from "./arrays.js";
        """,
        "strings.js": "export function slugify(s) { return s.toLowerCase().replace(/ /g, '-'); }",
        "arrays.js": "export function chunk(a, n) { const r=[]; for(let i=0;i<a.length;i+=n) r.push(a.slice(i,i+n)); return r; }",
    }),
    "@agent/config": "export const MAX_RETRIES = 3;",
})

with Runtime() as rt:
    with rt.new_context() as ctx:
        ctx.install(stdlib)

        @ctx.function
        async def _readFile(path: str) -> str:
            return open(path).read()

        await ctx.eval_async("""
            import { readFile } from "@agent/fs";
            import { slugify } from "@agent/utils";
            import { MAX_RETRIES } from "@agent/config";

            const raw = await readFile("input.txt");
            globalThis.result = slugify(raw);
        """, module=True)
```

## 2. Design principles

**One type.** `ModuleScope` is the only new public type. It's a frozen dict where keys are names and values are either JS source strings or nested `ModuleScope`s. No `ModuleRegistry`, no `HostModule`, no `Resolver`, no `Loader` — just data.

**Two levels max.** The outer `ModuleScope` maps import specifiers (`"@agent/fs"`, `"@agent/utils"`) to either a source string (single-file module) or a nested `ModuleScope` (multi-file scope with its own resolver space). Nested scopes map filenames (`"index.js"`, `"helpers.js"`) to source strings. No deeper nesting — a nested scope cannot contain further nested scopes.

**Scopes are resolver boundaries.** `./helpers.js` inside `@agent/fs` resolves within `@agent/fs`'s file map. `./helpers.js` inside `@agent/utils` resolves within `@agent/utils`'s file map. Same name, different files, no collision. `../` is not a valid specifier — there's no path relationship between scopes.

**Host functions stay as globals.** `@ctx.function` and `ctx.register()` are unchanged. JS modules reference host functions by their global names. This keeps the module system purely about JS-to-JS organization — Python callable registration is a separate concern that's already solved.

**Composition is dict operations.** Build module sets by spreading, merging, filtering, and overriding dicts. No method chains, no builder pattern, no special merge semantics. It's just `{**base.modules, "@agent/extra": ModuleScope({...})}`.

## 3. Python API

### 3.1 ModuleScope

```python
from quickjs_rs import ModuleScope

@dataclass(frozen=True)
class ModuleScope:
    """A composable module registry.

    Top-level: maps import specifiers to source strings or nested scopes.
    Nested: maps filenames to source strings (flat, no subdirectories).

    Examples:
        # Single-file module
        ModuleScope({"my-lib": "export const x = 1;"})

        # Multi-file scope
        ModuleScope({"my-lib": ModuleScope({
            "index.js": "export { foo } from './util.js';",
            "util.js": "export function foo() { return 1; }",
        })})
    """
    modules: dict[str, str | ModuleScope]
```

**Validation at construction:**

- Nested `ModuleScope` values must contain an `"index.js"` key (the entry point).
- Nested `ModuleScope` values must have only `str` values (no further nesting).
- Filename keys in a nested scope must not contain `/` (flat namespace).
- Top-level keys should not start with `./` or `../` (these are relative specifiers, not valid as scope names).

```python
# Valid
ModuleScope({"@agent/fs": ModuleScope({"index.js": "export default 1;"})})
ModuleScope({"lodash": "export function get(o, k) { return o[k]; }"})

# Invalid — no index.js
ModuleScope({"@agent/fs": ModuleScope({"helpers.js": "..."})})  # ValueError

# Invalid — nested too deep
ModuleScope({"a": ModuleScope({"index.js": ModuleScope({...})})})  # ValueError

# Invalid — subdirectory in nested scope
ModuleScope({"a": ModuleScope({"index.js": "...", "lib/x.js": "..."})})  # ValueError

# Invalid — relative path as top-level key
ModuleScope({"./local": "..."})  # ValueError
```

### 3.2 Context.install

```python
class Context:
    def install(self, scope: ModuleScope) -> None:
        """Install modules into this context, enabling import statements in module-mode eval.

        Additive: can be called multiple times. Each call inserts the scope's
        modules into the context's backing store. Modules from previous install()
        calls remain available.

        Caveat: if a module name has already been *imported* (executed by QuickJS),
        re-installing a different source under the same name has no effect — QuickJS
        caches module records per context and won't re-load a cached module.
        Installing a name that hasn't been imported yet works as expected.

        Host functions should be registered (via @ctx.function / ctx.register)
        before or after install — order doesn't matter, as long as they're
        registered before eval references them.
        """
```

### 3.3 Module-mode eval

When `module=True` on `eval` / `eval_async`:

- Code is parsed and evaluated as an ES module (`Module::declare` + `Module::eval`).
- `import` and `export` statements work.
- Top-level `await` works (ES modules have async completion semantics).
- `let`/`const`/`var`/function declarations at the top level are module-scoped, not global.
- Return value is `None` (ES modules complete with `undefined`).
- To get a value out: set `globalThis.result = ...` and read it with script-mode `ctx.eval("result")`.

When `module=False` (default):

- Script-mode eval, unchanged from v0.3.
- `import` statements are NOT available.
- State persists across calls (REPL semantics).
- Return value is the last expression.

```python
# Module mode: imports work, return value is None
await ctx.eval_async("""
    import { readFile } from "@agent/fs";
    globalThis.data = await readFile("x.txt");
""", module=True)

# Script mode: no imports, but you get a return value
length = ctx.eval("data.length")
```

### 3.4 Composition patterns

```python
# Base modules
base = ModuleScope({
    "@agent/utils": ModuleScope({
        "index.js": "export function id(x) { return x; }",
    }),
    "@agent/config": "export const ENV = 'production';",
})

# Add modules
full = ModuleScope({
    **base.modules,
    "@agent/fs": ModuleScope({
        "index.js": "export async function read(p) { return await _read(p); }",
    }),
    "@agent/http": ModuleScope({
        "index.js": "export async function fetch(u) { return await _fetch(u); }",
    }),
})

# Override for testing
test = ModuleScope({
    **full.modules,
    "@agent/config": "export const ENV = 'test';",
})

# Remove a module
no_http = ModuleScope({
    k: v for k, v in full.modules.items() if k != "@agent/http"
})

# Merge two independent module sets
team_a = ModuleScope({"@a/lib": "export const A = 1;"})
team_b = ModuleScope({"@b/lib": "export const B = 2;"})
combined = ModuleScope({**team_a.modules, **team_b.modules})
```

No special methods for composition. It's a frozen dict — use dict operations.

## 4. Resolver semantics

Two rules. No path normalization, no filesystem simulation.

### Rule 1: relative import (`./`)

When a file inside a nested scope imports with `./`:

```
referrer = "@agent/utils/strings.js"
specifier = "./helpers.js"
```

The resolver identifies the scope (`@agent/utils`), strips `./`, looks up `"helpers.js"` in that scope's file map. Found → canonical name is `"@agent/utils/helpers.js"`. Not found → error.

`../` is always an error. There's no parent to traverse to — scopes are closed namespaces.

### Rule 2: bare import (no `./`)

When any code imports without `./`:

```
referrer = "@agent/utils/strings.js"  (or "<eval>")
specifier = "@agent/config"
```

The resolver looks up `"@agent/config"` in the top-level scope. If the value is a string → that's the module source. If the value is a nested `ModuleScope` → resolve to its `"index.js"`.

This is how cross-scope imports work: `@agent/fs/index.js` can import from `@agent/utils` because bare specifiers always resolve at the top level.

### Resolution flowchart

```
import "X" from "Y":

  Is X relative? (starts with "./")
  ├── Yes
  │   ├── Is Y inside a scope? (Y = "scopeName/fileName")
  │   │   ├── Yes → strip "./" from X, look up in scope's file map
  │   │   │   ├── Found → canonical: "scopeName/X"
  │   │   │   └── Not found → Error: module not found
  │   │   └── No (Y = "<eval>") → Error: relative imports not available
  │   │                            in top-level eval
  │   └── Starts with "../" → Error: cannot escape module scope
  │
  └── No (bare specifier)
      └── Look up X in top-level scope
          ├── String value → X is the source
          ├── ModuleScope value → resolve to "X/index.js"
          └── Not found → Error: module not found
```

## 5. Rust extension additions

### 5.1 Cargo.toml

Add the `loader` feature to rquickjs:

```toml
rquickjs = { version = "0.11", features = [
    "classes",
    "properties",
    "futures",
    "bindgen",
    "loader",        # NEW
] }
```

### 5.2 Module resolver/loader (Rust side)

```rust
/// Flat registry populated from Python's ModuleScope at install time.
/// Keys are canonical names: "specifier" for single-file modules,
/// "specifier/filename" for files within a nested scope.
struct FlatModuleStore {
    /// canonical_name → JS source
    sources: RefCell<HashMap<String, String>>,
    /// scope_name → set of filenames (for nested scopes only)
    scopes: RefCell<HashMap<String, HashSet<String>>>,
}

impl Resolver for FlatModuleStore {
    fn resolve<'js>(&mut self, _ctx: &Ctx<'js>, base: &str, name: &str) -> Result<String> {
        if name.starts_with("../") {
            return Err(Error::new_resolving_message(
                base, name, "cannot use ../ to escape module scope",
            ));
        }

        if name.starts_with("./") {
            // Rule 1: relative import — must be inside a scope
            let scope_name = base.split('/').next()
                .ok_or_else(|| Error::new_resolving(base, name))?;

            // Verify referrer is in a scope
            let scopes = self.scopes.borrow();
            if !scopes.contains_key(scope_name) {
                return Err(Error::new_resolving_message(
                    base, name,
                    "relative imports require a module scope context",
                ));
            }

            let file = name.strip_prefix("./").unwrap();
            let canonical = format!("{}/{}", scope_name, file);

            if self.sources.borrow().contains_key(&canonical) {
                return Ok(canonical);
            }
            return Err(Error::new_resolving(base, name));
        }

        // Rule 2: bare specifier — top-level lookup
        // Direct match (single-file module)
        if self.sources.borrow().contains_key(name) {
            return Ok(name.to_string());
        }

        // Scope match (nested scope → index.js)
        let scopes = self.scopes.borrow();
        if scopes.contains_key(name) {
            let canonical = format!("{}/index.js", name);
            if self.sources.borrow().contains_key(&canonical) {
                return Ok(canonical);
            }
        }

        Err(Error::new_resolving(base, name))
    }
}

impl Loader for FlatModuleStore {
    fn load<'js>(&mut self, ctx: &Ctx<'js>, name: &str) -> Result<Module<'js>> {
        let sources = self.sources.borrow();
        let source = sources.get(name)
            .ok_or_else(|| Error::new_loading(name))?;
        Module::declare(ctx, name, source.as_str())
    }
}
```

### 5.3 New QjsContext methods

```rust
#[pymethods]
impl QjsContext {
    /// Register a single-file module (top-level string value).
    fn add_module_source(&self, canonical_name: &str, source: &str) -> PyResult<()>;

    /// Register a scope entry (nested ModuleScope file).
    fn add_scope_file(&self, scope_name: &str, filename: &str, source: &str) -> PyResult<()>;

    /// Evaluate code as an ES module.
    /// Returns a handle to the module namespace object.
    fn eval_module(&self, code: &str, filename: &str) -> PyResult<QjsHandle>;
}
```

The Python `Context.install()` calls these in a loop:

```python
def install(self, scope: ModuleScope) -> None:
    for name, value in scope.modules.items():
        if isinstance(value, str):
            self._engine_ctx.add_module_source(name, value)
        elif isinstance(value, ModuleScope):
            for filename, source in value.modules.items():
                self._engine_ctx.add_scope_file(name, filename, source)
```

Additive: each call inserts into the backing `FlatModuleStore`. No guard, no flag, no error on repeated calls. Re-inserting a name that hasn't been imported yet overwrites the previous source. Re-inserting a name that has been imported is a no-op (QuickJS module cache takes precedence).

### 5.4 Module evaluation path

When `Context.eval()` or `Context.eval_async()` is called with `module=True`:

```rust
fn eval_module(&self, code: &str, filename: &str) -> PyResult<QjsHandle> {
    self.with_active_ctx(|ctx| {
        let module = Module::declare(ctx.clone(), filename, code)
            .map_err(|e| js_error_to_py(e))?;

        let (module, promise) = module.eval()
            .map_err(|e| js_error_to_py(e))?;

        // Return the promise — the Python driving loop handles it
        // Module namespace is accessible via the module handle
        let promise_val: Value = promise.into_value();
        let persistent = Persistent::save(&ctx, promise_val);
        Ok(QjsHandle::new(persistent, self.context_id()))
    })
}
```

The Python side routes through the existing driving loop for async, or drains pending jobs for sync:

```python
async def eval_async(self, code, *, module=False, **kw):
    if module:
        handle = self._engine_ctx.eval_module(code, kw.get("filename", "<eval>"))
        # Module eval returns a promise that settles when evaluation completes
        # Drive it through the same _run_inside_task_group machinery as v0.3
        await self._drive_promise_handle(handle)
        return None  # ES modules complete with undefined
    else:
        # Existing script-mode path, unchanged
        ...
```

## 6. Module lifecycle

### 6.1 Registration → import → evaluation

```
Python: ctx.install(scope)
  → for each entry, calls add_module_source / add_scope_file
  → Rust: populates FlatModuleStore.sources and .scopes

Python: ctx.eval_async("import { readFile } from '@agent/fs'; ...", module=True)
  → Rust: Module::declare(ctx, "<eval>", code)
    → QuickJS parses, finds import "@agent/fs"
    → Resolver::resolve(base="<eval>", name="@agent/fs")
      → bare specifier → scopes has "@agent/fs" → Ok("@agent/fs/index.js")
    → Loader::load(name="@agent/fs/index.js")
      → Module::declare(ctx, "@agent/fs/index.js", source)
    → If @agent/fs/index.js has imports, resolve/load recursively
    → QuickJS links all modules, evaluates depth-first
  → Module::eval() → (Module, Promise)
  → Promise feeds into existing driving loop
```

### 6.2 Module caching

QuickJS caches module records per context. Once `@agent/fs` is imported, subsequent `import "@agent/fs"` returns the cached module — the loader is NOT called again. This is correct ES module behavior (modules are singletons per realm).

Consequence for additive `install()`: calling `ctx.install()` with a new source for a module name that's already been imported is a no-op — QuickJS serves the cached version. However, installing a module name that hasn't been imported yet works normally, even if other modules have already been imported. This means the pattern "install base modules, eval some code, install additional modules, eval more code" works correctly as long as the additional modules are genuinely new names.

### 6.3 Module + script interaction

Modules and scripts share the same global object within a context. Globals set by script-mode eval are visible in modules, and globals set by modules are visible in script-mode eval. Only `let`/`const`/`var` are module-scoped.

```python
ctx.eval("globalThis.apiKey = 'abc123'")

await ctx.eval_async("""
    import { fetch } from "@agent/http";
    await fetch("/api", { headers: { Authorization: apiKey } });
""", module=True)
```

This is the primary pattern for mixing modules (for imports) with scripts (for REPL-like eval with return values).

## 7. eval_async default change

**Breaking change from v0.3:** `eval_async`'s `module` parameter defaults to `False`, not `True`.

In v0.3, `module=True` was the default because it enabled top-level `await` via script-mode async eval. In v0.4, `module=True` means real ES module evaluation with different scoping semantics (module-scoped bindings, `None` return value). Silently changing the behavior of existing code that relied on script-mode persistence would be worse than requiring explicit opt-in.

Script mode (`module=False`) still supports top-level `await` via `JS_EVAL_FLAG_ASYNC`, same as v0.3.

```python
# v0.3 behavior (preserved under module=False):
await ctx.eval_async("const x = await fetch(); x")  # returns x

# v0.4 module mode (opt-in):
await ctx.eval_async("import { y } from '@agent/lib'; ...", module=True)  # returns None
```

Migration: any v0.3 code that relied on the `module=True` default and doesn't use `import` statements is unaffected — it was using script-mode semantics that `module=False` preserves. Code that was already passing `module=True` explicitly is also unaffected. Only code that relied on the default and also uses `import` (impossible in v0.3 since imports didn't work) is affected — and that code didn't exist.

## 8. Errors

No new exception classes. Module errors surface through existing types:

| Error condition | Exception |
|---|---|
| Syntax error in module source | `JSError(name="SyntaxError", ...)` |
| Module not found | `JSError(name="Error", message="Could not load module '...'")` |
| Attempting `../` escape | `JSError(name="Error", message="cannot use ../ to escape module scope")` |
| Relative import from top-level eval | `JSError(name="Error", message="relative imports require a module scope context")` |
| Module evaluation throws | `JSError` (or `TimeoutError`, `MemoryLimitError` — same classification as eval) |
| Circular import | Not an error — ES modules handle cycles via live bindings |
| Re-install after import | Not an error — silently ignored (QuickJS module cache takes precedence) |

## 9. Testing

### 9.1 New test file

`tests/test_modules.py`:

**Registration and basic import:**
- Single-file module: register string, import constant
- Multi-file scope: register scope, import from index.js
- Internal imports: index.js imports ./helpers.js within scope
- Transitive internal imports: a.js → ./b.js → ./c.js within scope

**Resolver boundary enforcement:**
- `./` in scope A resolves within A, not B (same filename, different scopes)
- `./` from top-level eval raises error
- `../` always raises error
- Bare specifier from inside a scope resolves at top level (cross-scope import)

**Module evaluation semantics:**
- `module=True` returns `None`
- `let` in module is not visible in subsequent eval
- `globalThis.x` in module IS visible in script-mode eval
- Script-mode eval can read globals set by module-mode eval

**Async modules:**
- Module uses top-level `await` with async host function
- Module imports from another module that uses `await`

**Composition:**
- Dict spread creates valid new scope
- Override replaces a module
- Removal via dict comprehension

**Additive install:**
- Two `install()` calls, modules from both are importable
- `install()` after eval — newly installed module is importable in subsequent eval
- Re-install same name before import — new source takes effect
- Re-install same name after import — no effect (QuickJS cache)

**Error cases:**
- Import non-registered module → JSError
- Syntax error in module source → JSError
- `../` escape attempt → JSError
- Nested scope missing index.js → ValueError (at construction)
- `/` in nested scope filename → ValueError (at construction)

**Cross-scope imports:**
- Module in scope A imports from scope B (both installed)
- Module in scope A imports from scope B (B not installed) → JSError at eval time

**Module caching:**
- Import module, re-register source, import again → gets cached version

### 9.2 Acceptance test (§13.3)

```python
async def test_module_acceptance():
    stdlib = ModuleScope({
        "@agent/config": "export const MAX_RETRIES = 3;",
        "@agent/utils": ModuleScope({
            "index.js": """
                export { slugify } from "./strings.js";
                export { chunk } from "./arrays.js";
            """,
            "strings.js": """
                export function slugify(s) {
                    return s.toLowerCase().replace(/ /g, '-');
                }
            """,
            "arrays.js": """
                export function chunk(arr, size) {
                    const r = [];
                    for (let i = 0; i < arr.length; i += size)
                        r.push(arr.slice(i, i + size));
                    return r;
                }
            """,
        }),
        "@agent/fs": ModuleScope({
            "index.js": """
                export async function readFile(path) {
                    return await _readFile(path);
                }
            """,
        }),
        "@agent/concurrency": ModuleScope({
            "index.js": """
                export async function swarm(tasks, opts) {
                    return await _swarm(tasks, opts.concurrency || 10);
                }
            """,
        }),
    })

    with Runtime() as rt:
        with rt.new_context() as ctx:
            @ctx.function
            async def _readFile(path: str) -> str:
                return "Date: 2024-01-01\nDate: 2024-01-02\nNotDate"

            @ctx.function
            async def _swarm(tasks: list, concurrency: int) -> dict:
                return {
                    "completed": len(tasks),
                    "failed": 0,
                    "results": [
                        {"id": t["id"], "status": "completed",
                         "result": '{"abbreviation_count": 1}'}
                        for t in tasks
                    ],
                }

            ctx.install(stdlib)

            # The motivating pattern — with real imports
            await ctx.eval_async("""
                import { readFile } from "@agent/fs";
                import { swarm } from "@agent/concurrency";
                import { chunk } from "@agent/utils";
                import { MAX_RETRIES } from "@agent/config";

                const raw = await readFile("/context.txt");
                const lines = raw.split("\\n").filter(l => l.startsWith("Date:"));
                const chunks = chunk(lines, 50);
                const tasks = chunks.map((c, i) => ({
                    id: `chunk_${i}`,
                    description: c.join("\\n"),
                }));
                const summary = await swarm(tasks, { concurrency: 32 });

                let total = 0;
                for (const r of summary.results) {
                    if (r.status === "completed") {
                        total += JSON.parse(r.result).abbreviation_count;
                    }
                }
                globalThis.result = { total, retries: MAX_RETRIES };
            """, module=True)

            result = ctx.eval("result")
            assert result["total"] == 1
            assert result["retries"] == 3

            # Cross-scope import works
            await ctx.eval_async("""
                import { slugify } from "@agent/utils";
                globalThis.slug = slugify("Hello World");
            """, module=True)
            assert ctx.eval("slug") == "hello-world"

            # Module scope isolation
            await ctx.eval_async("let moduleLocal = 42;", module=True)
            assert ctx.eval("typeof moduleLocal") == "undefined"

            # Globals bridge
            ctx.globals["pyValue"] = "from python"
            await ctx.eval_async("""
                globalThis.bridged = pyValue + " via module";
            """, module=True)
            assert ctx.eval("bridged") == "from python via module"

            # Resolver boundary: ./strings.js in @agent/utils is
            # NOT the same as ./strings.js would be in @agent/fs
            # (if @agent/fs had one). Scopes are isolated.

        # Composition: override for testing
        test_stdlib = ModuleScope({
            **stdlib.modules,
            "@agent/config": "export const MAX_RETRIES = 1;",
        })

        with rt.new_context() as ctx:
            @ctx.function
            async def _readFile(path: str) -> str:
                return "mock"

            @ctx.function
            async def _swarm(tasks: list, concurrency: int) -> dict:
                return {"completed": 0, "failed": 0, "results": []}

            ctx.install(test_stdlib)

            await ctx.eval_async("""
                import { MAX_RETRIES } from "@agent/config";
                globalThis.retries = MAX_RETRIES;
            """, module=True)
            assert ctx.eval("retries") == 1  # overridden

        # Capability restriction: no @agent/fs
        restricted = ModuleScope({
            k: v for k, v in stdlib.modules.items()
            if k != "@agent/fs"
        })

        with rt.new_context() as ctx:
            ctx.install(restricted)
            try:
                await ctx.eval_async("""
                    import { readFile } from "@agent/fs";
                """, module=True)
                assert False, "should have raised"
            except Exception as e:
                assert "Could not" in str(e) or "resolve" in str(e).lower()
```

## 10. Implementation order

1. **Cargo.toml:** add `loader` feature. Verify build. Commit as `build: enable rquickjs loader feature`.

2. **ModuleScope class (Python only):** frozen dataclass with validation. No Rust changes. Commit as `api: ModuleScope type with validation`.

3. **Static registry + install:** implement `FlatModuleStore` in Rust (Resolver + Loader), `add_module_source`, `add_scope_file`. Wire `Context.install()` in Python. First test: single-file module, import a constant. Commit as `engine+api: module registry — install + single-file import`.

4. **Nested scopes:** wire `add_scope_file` for multi-file scopes. Test: scope with index.js + internal ./helpers.js import. Commit as `engine: nested scope resolution — ./relative imports within scopes`.

5. **Resolver boundary enforcement:** test `../` rejection, `./` from top-level rejection, cross-scope bare imports. Commit as `engine: resolver boundaries — ../ rejection, cross-scope imports`.

6. **Module evaluation path:** implement `eval_module` in Rust, wire `module=True` in Python's `eval` / `eval_async` to route through module evaluation. Change `module` default to `False`. Test: module-scoped let, globalThis bridge, async module with host function. Commit as `engine+api: module evaluation — module=True uses real ES modules`.

7. **Full test suite + acceptance:** fill out `test_modules.py` per §9.1, add §13.3 acceptance test to `test_smoke.py`. Commit as `tests: module loading — full coverage + §13.3 acceptance`.

8. **Spec + CLAUDE.md updates.** Tag v0.4.0-rc1.

## 11. Open decisions

- **Pre-parse on install?** If `add_module_source` / `add_scope_file` parses the source immediately, syntax errors surface at `install()` time rather than at first `import`. Pro: fail-fast. Con: requires a live `Ctx` during install, which may not align with rquickjs's lifecycle. Lean: defer parsing to first import, document that syntax errors surface at eval time.

- **eval_handle_async with module=True: what does the handle point to?** Two options: (a) the module namespace object (has exports as properties), (b) the evaluation promise. Lean: module namespace object — that's what users want to inspect. The promise is internal machinery.

- **Module names: any string or namespaced?** The spec allows any string as a module name (`"lodash"`, `"@agent/fs"`, `"my-thing"`). No enforcement of `@scope/name` convention. Users pick their own naming. This is simpler but means collisions between independently-authored module sets are the user's problem. Lean: keep it open, document the `@scope/name` convention as recommended but not enforced.

- **Install on Runtime vs Context?** Resolved: `ctx.install()` is the API, but rquickjs's `set_loader` operates at the Runtime level. The `FlatModuleStore` is set on the runtime at creation and shared across all contexts on that runtime. Multiple `install()` calls from any context add to the same backing store. This means all contexts on the same runtime see the same module set. If different contexts need different module sets, use separate runtimes (8.6 µs each). This is acceptable for v0.4 — per-context module filtering is a v0.5 concern if it ever surfaces as a real need.

## 12. Out of scope for v0.4

- Dynamic/async module resolver (sync static registry only)
- npm / JSR module resolution
- Filesystem-based module loading
- Module hot-reloading within a context
- `import.meta` population
- Source maps
- Module bytecode compilation / caching
- Three-level-deep nesting in ModuleScope
- `HostModule` as a separate type (host functions stay as globals)
