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

**Recursive nesting.** A `ModuleScope` can contain both `str` values (its own files) and other `ModuleScope` values (its named dependencies) at any depth. A dependency can itself carry dependencies that carry dependencies, for as many levels as the dependency graph needs. The earlier "two levels max" rule is gone.

**Scopes are self-contained resolver boundaries.** Each scope resolves only within its own dict. Code inside a scope imports its own files via `./filename`, and its named dependencies via bare specifier — both looked up in the scope's dict, nowhere else. No sibling visibility, no parent traversal, no top-level fallback. A scope that uses a dependency must carry it directly. To share a dependency across scopes, spread it into each scope's dict (`**utils.modules`).

**Host functions stay as globals.** `@ctx.function` and `ctx.register()` are unchanged. JS modules reference host functions by their global names. This keeps the module system purely about JS-to-JS organization — Python callable registration is a separate concern that's already solved.

**Composition is dict operations.** Build module sets by spreading, merging, filtering, and overriding dicts. No method chains, no builder pattern, no special merge semantics. The self-containment property makes this work at any level: `{**base.modules, "@extra": ModuleScope({**base.modules, "index.js": "..."})}` just works.

## 3. Python API

### 3.1 ModuleScope

```python
from quickjs_rs import ModuleScope

@dataclass(frozen=True)
class ModuleScope:
    """A composable, recursive module registry.

    A ModuleScope is a dict keyed on name, valued by either a JS
    source string (a file belonging to this scope) or another
    ModuleScope (a named dependency of this scope). Dependencies can
    themselves carry further dependencies; nesting is unbounded.

    Each scope is a closed resolver namespace. Code inside the scope
    can import only what the scope's dict contains — its own files
    via ./filename, its named dependencies via bare specifier. A
    scope cannot see its siblings, its parent, or its transitive
    dependencies unless they're directly listed in its dict.

    To share dependencies, spread them explicitly:

        stdlib = ModuleScope({
            "@agent/utils": ModuleScope({
                "index.js": "export function slugify(s) {...}",
            }),
        })

        main = ModuleScope({
            **stdlib.modules,
            "@agent/fs": ModuleScope({
                **stdlib.modules,   # @agent/fs carries its own deps
                "index.js": '''
                    import { slugify } from "@agent/utils";
                    // resolves within @agent/fs's own dict
                '''
            }),
        })

    Examples:
        # Single-file module at top level
        ModuleScope({"my-lib": "export const x = 1;"})

        # Multi-file scope
        ModuleScope({"my-lib": ModuleScope({
            "index.js": "export { foo } from './util.js';",
            "util.js": "export function foo() { return 1; }",
        })})

        # Scope with a dependency
        ModuleScope({"my-lib": ModuleScope({
            "@peer": "export const P = 1;",
            "index.js": 'import { P } from "@peer"; export const x = P;',
        })})
    """
    modules: dict[str, str | ModuleScope]
```

**Validation at construction:**

- A ModuleScope that contains at least one `str` value must include an `"index.js"` entry — that's what a bare `import ... from '<scope-name>'` resolves to from the parent scope.
- Scopes that contain only other `ModuleScope` values (i.e. pure dependency containers, like the top-level `main` above) do not need `index.js`. They aren't themselves importable targets; they're just the outer registry scope.
- Filename keys for `str` entries must not contain `/` (flat namespace within a scope). Subdirectories aren't representable — introduce a nested scope instead.
- Top-level keys (and any key at any depth) must not start with `./` or `../` (those are relative import specifiers, not valid as scope names).
- Nested values of type `ModuleScope` may themselves contain `str` values, further `ModuleScope` values, or a mix. Nesting is recursive without a depth limit.

```python
# Valid — scope with a single file
ModuleScope({"@agent/fs": ModuleScope({"index.js": "export default 1;"})})

# Valid — single-file module at top level
ModuleScope({"lodash": "export function get(o, k) { return o[k]; }"})

# Valid — recursive nesting: main scope containing a dep scope
#         which carries its own dep scope
ModuleScope({
    "@agent/utils": "export const U = 1;",
    "@agent/fs": ModuleScope({
        "@agent/utils": "export const U = 1;",
        "@agent/log": ModuleScope({
            "index.js": "export function log(x) { console.log(x); }",
        }),
        "index.js": '''
            import { U } from "@agent/utils";
            import { log } from "@agent/log";
            export async function readFile(p) { log(p); return _readFile(p); }
        ''',
    }),
})

# Valid — pure-dependency container (no str values → no index.js
#         required). This is the typical shape of the "outer"
#         scope you hand to ctx.install.
ModuleScope({
    "@agent/utils": ModuleScope({"index.js": "export const U = 1;"}),
    "@agent/fs": ModuleScope({"index.js": "export const F = 2;"}),
})

# Invalid — scope has str entries but no index.js
ModuleScope({"@agent/fs": ModuleScope({"helpers.js": "..."})})  # ValueError

# Invalid — subdirectory in a str-keyed entry
ModuleScope({"a": ModuleScope({"index.js": "...", "lib/x.js": "..."})})  # ValueError

# Invalid — relative specifier as a key
ModuleScope({"./local": "..."})  # ValueError
```

The old "two levels max" rule from earlier drafts is gone. A scope can nest as deeply as the dependency graph requires. The only structural cap is recursion depth in the Python interpreter (ModuleScope validation is naturally bounded by whatever `sys.getrecursionlimit()` permits, which is thousands).

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

One rule. No path normalization, no filesystem simulation, no parent traversal, no sibling visibility, no top-level fallback. Each scope resolves only within its own dict.

### The rule

Every module has a containing scope. For a scope file `"scope/subscope/.../filename.js"`, the containing scope is `"scope/subscope/..."`. For the top-level eval (`"<eval>"`), the containing scope is the root scope installed via `ctx.install`.

When code in a referrer imports a specifier:

1. Identify the referrer's containing scope.
2. If specifier starts with `./`, strip it and look up the remainder as a `str` key in that scope's dict. Found → canonical name is `"{containing_scope}/{filename}"`. Not found → resolution error.
3. If specifier is bare (no `./`), look up the whole specifier as a `ModuleScope` key in that scope's dict. Found → canonical name is `"{containing_scope}/{specifier}/index.js"` (the scope's entry point). Not found → resolution error.
4. If specifier starts with `../`, that's always an error. Scopes are closed; there is no parent.

No fallback to the root scope from inside a nested scope. If code in `@agent/fs/index.js` imports `"@agent/utils"`, the resolver looks up `"@agent/utils"` in `@agent/fs`'s own dict. If `@agent/fs` doesn't carry `@agent/utils` as a dependency, the import fails — even if the outer scope does carry it. This is the self-containment property: a scope's dependency surface is exactly what its dict declares.

To share a dependency across multiple scopes, spread it into each scope's dict:

```python
utils = {"@agent/utils": ModuleScope({"index.js": "export const U = 1;"})}

main = ModuleScope({
    **utils,
    "@agent/fs": ModuleScope({
        **utils,           # @agent/fs's own import of @agent/utils
        "index.js": "import { U } from '@agent/utils'; ...",
    }),
    "@agent/http": ModuleScope({
        **utils,           # independent reference to the same source
        "index.js": "import { U } from '@agent/utils'; ...",
    }),
})
```

The same source is registered under multiple canonical names — `"@agent/utils/index.js"` (outer) and `"@agent/fs/@agent/utils/index.js"` (inner) and `"@agent/http/@agent/utils/index.js"` (inner). QuickJS caches each canonical-named module independently; the instances don't collide because the cache key is the canonical path.

### Resolution flowchart

```
import "X" from "Y":

  Y's containing scope = S (the scope whose dict directly holds Y).
  For Y == "<eval>", S is the root scope.

  Is X relative (starts with "./")?
  ├── Yes
  │   ├── Strip "./", look up remainder in S's dict
  │   │   ├── Found as str → canonical: "S/X-without-./"
  │   │   └── Not found, or found as ModuleScope → Error: module not found
  │   └── Starts with "../" → Error: cannot escape module scope
  │
  └── No (bare specifier)
      └── Look up X in S's dict
          ├── Found as ModuleScope → canonical: "S/X/index.js"
          ├── Found as str → Error: bare specifier resolved to a file,
          │                  not a scope (use ./X to import a file)
          └── Not found → Error: module not found
```

`"<eval>"` is the referrer for code passed to `ctx.eval_async(module=True)`. Its containing scope is the root scope installed via `ctx.install`, so top-level eval can import anything the root scope declares. If the root scope is a pure-dependency container (`ModuleScope({...scopes only...})`), relative (`./`) imports from the eval body fail — the root has no `str` entries.

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

The backing store holds a flattened view of the scope tree. At install time, Python walks the `ModuleScope` recursively and inserts:

* Each `str` entry (source file) under a canonical path that concatenates the scope-path segments plus the filename: `"scope1/scope2/.../filename.js"`.
* Each `ModuleScope` entry as a scope entry whose key is the scope-path: `"scope1/scope2/..."`. Scope entries also store the set of child keys (direct members of the scope's dict) so the resolver can answer "does this scope contain X?" without rescanning.

The root scope (the one passed to `ctx.install`) has the empty scope-path. A file at the root shows up as `"filename.js"` (root-scope path `""` + `"/"` elided + filename). A file inside `@agent/fs` shows up as `"@agent/fs/filename.js"`. A file inside `@agent/fs/@peer/lib` shows up as `"@agent/fs/@peer/lib/filename.js"`.

```rust
/// Tree registry populated from Python's ModuleScope at install time.
/// The tree is flattened into two HashMaps keyed on canonical scope
/// paths (joined with '/'); the root scope has the empty path "".
struct FlatModuleStore {
    /// canonical_file_path → JS source.
    ///   Example keys:
    ///     "lodash"                            (str at root)
    ///     "@agent/fs/index.js"                (file in root-level scope)
    ///     "@agent/fs/@peer/lib/index.js"      (file in nested-dep scope)
    sources: HashMap<String, String>,

    /// canonical_scope_path → set of the scope's direct dict keys
    /// (both str-valued and ModuleScope-valued children). Used by
    /// the resolver to check "does S have key K?" and to
    /// disambiguate bare-X resolves (scope child → X/index.js) from
    /// ./X resolves (str child → scope/X).
    ///   Example entries:
    ///     ""                     → {"lodash", "@agent/fs", ...}
    ///     "@agent/fs"            → {"@peer", "index.js", "util.js"}
    ///     "@agent/fs/@peer/lib"  → {"index.js"}
    scopes: HashMap<String, HashSet<String>>,

    /// canonical_scope_path → kind of each child key. Values: "str"
    /// for file children, "scope" for ModuleScope children. The
    /// resolver needs this to distinguish a bare-X that resolves
    /// to scope-X/index.js from a bare-X that's a file (an error).
    scope_child_kinds: HashMap<String, HashMap<String, ChildKind>>,
}

enum ChildKind { File, Scope }

impl Resolver for FlatModuleStore {
    /// `base` is the referrer's canonical path (a file path like
    /// "@agent/fs/index.js" or "<eval>" for top-level eval).
    /// `name` is the import specifier as written in the source.
    fn resolve<'js>(&mut self, _ctx: &Ctx<'js>, base: &str, name: &str) -> Result<String> {
        if name.starts_with("../") {
            return Err(Error::new_resolving_message(
                base, name, "cannot use ../ to escape module scope",
            ));
        }

        // Identify the referrer's containing scope. For a file path
        // "A/B/C/filename.js" the containing scope path is
        // "A/B/C". For "<eval>" the containing scope is the root
        // (empty path "").
        let containing_scope: String = if base == "<eval>" {
            String::new()
        } else {
            match base.rsplit_once('/') {
                Some((parent, _file)) => parent.to_string(),
                None => String::new(),  // file at root
            }
        };

        let scope_children = self.scope_child_kinds
            .get(&containing_scope)
            .ok_or_else(|| Error::new_resolving(base, name))?;

        if let Some(relative) = name.strip_prefix("./") {
            // Relative import — the key must be a str-valued child
            // of the containing scope.
            match scope_children.get(relative) {
                Some(ChildKind::File) => {
                    let canonical = join_path(&containing_scope, relative);
                    Ok(canonical)
                }
                _ => Err(Error::new_resolving(base, name)),
            }
        } else {
            // Bare import — the key must be a ModuleScope child of
            // the containing scope. Resolves to that scope's
            // index.js.
            match scope_children.get(name) {
                Some(ChildKind::Scope) => {
                    let scope_path = join_path(&containing_scope, name);
                    Ok(format!("{}/index.js", scope_path))
                }
                Some(ChildKind::File) => Err(Error::new_resolving_message(
                    base, name,
                    "bare specifier resolved to a file; use ./X to import a file",
                )),
                None => Err(Error::new_resolving(base, name)),
            }
        }
    }
}

/// `join_path("", "x") == "x"`, `join_path("a/b", "c") == "a/b/c"`.
/// The root-scope special case keeps files at root (which have no
/// leading slash in their canonical names) consistent with the
/// containing-scope convention.
fn join_path(scope: &str, child: &str) -> String {
    if scope.is_empty() { child.to_string() } else { format!("{}/{}", scope, child) }
}

impl Loader for FlatModuleStore {
    fn load<'js>(&mut self, ctx: &Ctx<'js>, name: &str) -> Result<Module<'js>> {
        let source = self.sources.get(name)
            .ok_or_else(|| Error::new_loading(name))?
            .clone();
        Module::declare(ctx.clone(), name, source)
    }
}
```

The backing store construction (from `Context.install`) walks the `ModuleScope` recursively:

```python
def _install_scope(self, scope: ModuleScope, scope_path: str) -> None:
    # Register scope membership.
    self._engine_ctx.declare_scope(scope_path, list(scope.modules.keys()),
                                   [kind(v) for v in scope.modules.values()])
    # Recurse into children.
    for name, value in scope.modules.items():
        child_path = join_path(scope_path, name)
        if isinstance(value, str):
            self._engine_ctx.add_source(child_path, value)
        else:  # ModuleScope
            self._install_scope(value, child_path)

def install(self, scope: ModuleScope) -> None:
    self._install_scope(scope, "")   # root scope has empty path
```

This produces three flat maps keyed on canonical paths — the same tree information, rewritten in a form the resolver can query in O(1).

### 5.3 New QjsRuntime / QjsContext methods

The backing `FlatModuleStore` lives on the runtime (§11 open-decision 4 — `set_loader` is per-runtime in rquickjs). Two methods on `QjsRuntime` mutate the store at install time; one method on `QjsContext` evaluates code as an ES module.

```rust
#[pymethods]
impl QjsRuntime {
    /// Register a file at `canonical_path`. For root-level files
    /// this is just the filename; for nested-scope files this is
    /// the joined scope-path plus filename (e.g.
    /// "@agent/fs/@peer/lib/index.js"). Called by Python's
    /// `Context.install` recursion for each str-valued entry.
    fn add_source(&self, canonical_path: &str, source: &str) -> PyResult<()>;

    /// Declare a scope's direct dict keys + kinds. Called for each
    /// ModuleScope (including the root) at install time. `kinds`
    /// is a list of "file" or "scope" strings aligned with `keys`.
    /// Used by the resolver to distinguish bare-X → scope/index.js
    /// from bare-X → error when X is a file.
    fn declare_scope(
        &self,
        scope_path: &str,
        keys: Vec<String>,
        kinds: Vec<String>,
    ) -> PyResult<()>;
}

#[pymethods]
impl QjsContext {
    /// Evaluate code as an ES module. Returns None (modules
    /// complete with undefined). Top-level await settles via the
    /// existing async driving loop (step 6).
    fn eval_module(&self, code: &str, filename: &str) -> PyResult<()>;
}
```

The Python `Context.install()` walks the scope tree recursively:

```python
def install(self, scope: ModuleScope) -> None:
    self._install_scope(scope, "")  # "" == root scope path

def _install_scope(self, scope: ModuleScope, scope_path: str) -> None:
    # Declare the scope's membership up front so the resolver can
    # answer "does S contain X?" before any source is loaded.
    keys = list(scope.modules.keys())
    kinds = [
        "scope" if isinstance(v, ModuleScope) else "file"
        for v in scope.modules.values()
    ]
    self._runtime._engine_rt.declare_scope(scope_path, keys, kinds)

    # Recurse into each child.
    for name, value in scope.modules.items():
        child_path = name if scope_path == "" else f"{scope_path}/{name}"
        if isinstance(value, str):
            self._runtime._engine_rt.add_source(child_path, value)
        else:  # ModuleScope
            self._install_scope(value, child_path)
```

Additive: each call inserts into the backing `FlatModuleStore`. No guard, no flag, no error on repeated calls. Re-inserting a name that hasn't been imported yet overwrites the previous source. Re-inserting a name that has been imported is a no-op (QuickJS module cache takes precedence). A second `install` with a different root ModuleScope also merges into the same store — users who want isolated module sets should use separate runtimes.

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
  → recurse the scope tree, calling declare_scope at each scope
    and add_source at each str entry
  → Rust: FlatModuleStore.sources + .scopes + .scope_child_kinds
    get populated with canonical-path keys

Python: ctx.eval_async("import { readFile } from '@agent/fs'; ...", module=True)
  → Rust: Module::declare(ctx, "<eval>", code)
    → QuickJS parses, finds import "@agent/fs"
    → Resolver::resolve(base="<eval>", name="@agent/fs")
      → containing scope of "<eval>" is the root (path "")
      → bare specifier → look up "@agent/fs" in root's child map
      → found as Scope → Ok("@agent/fs/index.js")
    → Loader::load(name="@agent/fs/index.js")
      → Module::declare(ctx, "@agent/fs/index.js", source)
    → @agent/fs/index.js has `import { slugify } from "@agent/utils";`
      → Resolver::resolve(base="@agent/fs/index.js", name="@agent/utils")
      → containing scope of base is "@agent/fs"
      → look up "@agent/utils" in @agent/fs's child map
      → found (because @agent/fs's dict carries @agent/utils)
        → Ok("@agent/fs/@agent/utils/index.js")
      → NOT the outer "@agent/utils/index.js" — self-containment
    → QuickJS links all modules, evaluates depth-first
  → Module::eval() → (Module, Promise)
  → Promise feeds into existing driving loop (step 6 integration)
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
- Single-file module: register string, import constant.
- Multi-file scope: register scope, import from index.js.
- Internal imports: index.js imports ./helpers.js within scope.
- Transitive internal imports: a.js → ./b.js → ./c.js within scope.
- Pure-dependency root: root ModuleScope contains only nested scopes (no str entries, no index.js at root); eval imports one of the scopes via bare specifier.

**Resolver — scope-local lookup (§4):**
- Bare specifier resolves only within the containing scope's dict — not in the outer scope, not in a sibling. A scope that imports a dependency must carry it in its own dict.
- `./X` in scope A resolves to A's own `X.js`; not to B's, even if B has an `X.js`.
- `./X` from top-level eval: works when the root scope has `X.js` as a `str` entry; errors when the root is a pure-dependency container (no file at root called `X.js`).
- `../` always errors.
- A bare specifier that resolves to a `str` (a file) errors with a message nudging toward `./X`. Bare specifiers must name a scope.
- A scope that uses a bare specifier for a dep it doesn't carry: import errors at eval time, even if an outer/parent/sibling scope does carry that dep.

**Recursive dependencies (new, §3.1 / §4):**
- Scope A carries scope B. A's index.js imports B via bare specifier → works.
- A also has a self-peer: `**A.modules` spread into A creates no import cycle; A's top-level and A-as-seen-from-A both resolve.
- Scope A carries scope B; B carries scope C. A's index.js → imports B → imports C. The chain works because each link carries its own dep.
- Scope A carries B; B carries C. A directly imports C → error (A doesn't carry C, even though A's dep B does). Self-containment: transitive deps are not visible.
- Same source appears under multiple canonical paths (spread into multiple scopes). Each canonical path resolves to the same source; QuickJS caches them as independent module records (assertion: both paths evaluate, each gets its own module instance).
- Depth-5+ nesting works: A → B → C → D → E, each importing the next via bare specifier. No recursion-limit artifacts in either validation or resolution.

**Module evaluation semantics:**
- `module=True` returns `None`.
- `let` in module is not visible in subsequent eval.
- `globalThis.x` in module IS visible in script-mode eval.
- Script-mode eval can read globals set by module-mode eval.

**Async modules:**
- Module uses top-level `await` with async host function.
- Module imports from another module that uses `await`.

**Composition:**
- Dict spread creates valid new scope.
- Override replaces a module.
- Removal via dict comprehension.
- Recursive spread: `**outer.modules` inside a nested scope creates a self-contained sub-scope that carries the same deps the outer carries.

**Additive install:**
- Two `install()` calls, modules from both are importable.
- `install()` after eval — newly installed module is importable in subsequent eval.
- Re-install same name before import — new source takes effect.
- Re-install same name after import — no effect (QuickJS cache).

**Error cases:**
- Import non-registered module → JSError.
- Import carried-by-ancestor-but-not-self dep from inside a scope → JSError (self-containment).
- Syntax error in module source → JSError.
- `../` escape attempt → JSError.
- Bare specifier resolving to a file → JSError with "use ./X" message.
- Scope with str entries missing index.js → ValueError at ModuleScope construction.
- `/` in a str-keyed filename → ValueError at construction.

**Module caching:**
- Import module, re-register source, import again → gets cached version.
- Same source registered under two canonical paths (via spread) → two cached records, two module instances (singleton-per-canonical-path, not singleton-per-source).

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

        # Self-contained recursive deps. @app carries @agent/utils
        # directly; @app's index.js imports it. A sibling eval body
        # that doesn't have @agent/utils in its own (root) scope
        # cannot reach into @app to borrow it — self-containment.
        utils = ModuleScope({
            "@agent/utils": ModuleScope({
                "index.js": "export function greet(n) { return 'hi ' + n; }",
            }),
        })
        recursive = ModuleScope({
            "@app": ModuleScope({
                **utils.modules,  # @app carries @agent/utils
                "index.js": """
                    import { greet } from "@agent/utils";
                    export const message = greet("world");
                """,
            }),
            # Note: the root does NOT carry @agent/utils. Eval from
            # root can only import @app; it cannot import
            # @agent/utils directly because root doesn't declare it.
        })
        with rt.new_context() as ctx:
            ctx.install(recursive)
            await ctx.eval_async("""
                import { message } from "@app";
                globalThis.msg = message;
            """, module=True)
            assert ctx.eval("msg") == "hi world"

            # Self-containment: the root scope does not itself
            # carry @agent/utils, so eval from root cannot import it
            # — even though @app does carry it.
            try:
                await ctx.eval_async("""
                    import { greet } from "@agent/utils";
                """, module=True)
                assert False, "self-containment should have blocked this"
            except Exception as e:
                assert "resolve" in str(e).lower() or "not found" in str(e).lower()
```

## 10. Implementation order

1. **Cargo.toml:** add `loader` feature. Verify build. Commit as `build: enable rquickjs loader feature`.

2. **ModuleScope class (Python only):** frozen dataclass with validation. No Rust changes. Commit as `api: ModuleScope type with validation`.

3. **Static registry + install:** implement `FlatModuleStore` in Rust (Resolver + Loader) with the scope-local resolver from §4. Expose `add_source` + `declare_scope` on `QjsRuntime`. Wire `Context.install()` as a recursive walk in Python. First test: single-file module at root, import a constant. Commit as `engine+api: module registry — install + single-file import`.

4. **Nested scopes and self-contained deps:** extend tests for multi-file scopes with internal `./x.js` imports and for scopes that carry their own dependency scopes. The resolver already handles both from step 3 — this step is primarily test coverage + docs. Commit as `engine: nested scope resolution — ./relative + recursive deps`.

5. **Resolver boundary enforcement:** test `../` rejection, `./` from pure-container root, self-containment (A carries B carries C; A cannot import C directly), bare-specifier-to-file error. Commit as `engine: resolver boundaries — self-containment + error cases`.

6. **Module evaluation path:** implement `eval_module` in Rust; wire `module=True` in Python's `eval` / `eval_async` to route through module evaluation. Change `eval_async` `module` default to `False`. Wire the Promise returned by `Module::eval` into the existing async driving loop for top-level-await modules. Test: module-scoped let, globalThis bridge, async module with host function. Commit as `engine+api: module evaluation — module=True uses real ES modules`.

7. **Full test suite + acceptance:** fill out `test_modules.py` per §9.1, add §13.3 acceptance test to `test_smoke.py`. Commit as `tests: module loading — full coverage + §13.3 acceptance`.

8. **Spec + CLAUDE.md updates.** Tag v0.4.0-rc1.

## 11. Open decisions

- **Pre-parse on install?** If `add_source` parses the source immediately at install time, syntax errors surface at `install()` rather than at first `import`. Pro: fail-fast. Con: requires a live `Ctx` during install, which may not align with rquickjs's lifecycle. Lean: defer parsing to first import, document that syntax errors surface at eval time.

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
- `HostModule` as a separate type (host functions stay as globals)
- Per-scope module cache invalidation (QuickJS caches per canonical path — good enough)
- Cross-runtime module sharing (each runtime has its own store; this is by design)
