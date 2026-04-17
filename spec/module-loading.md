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
    """A recursive, self-contained module registry.

    A ModuleScope holds two kinds of entries, keyed by value type:

      * `str` values — the scope's own files. Keys are POSIX-style
        paths; `/` in a key creates a subdirectory structure within
        the scope (``"lib/util.js"``, ``"tests/deep/nested.js"``).
      * `ModuleScope` values — the scope's named dependencies. Keys
        are bare import specifiers (``"@agent/fs"``, ``"lodash"``)
        that JS code will use to import the dep.

    The two namespaces don't mix. Relative specifiers (``./X``,
    ``../X``) match only `str` entries via POSIX path
    normalization. Bare specifiers match only `ModuleScope` entries.
    This means a ModuleScope can have an ``"index.js"`` str AND an
    ``"index.js"`` ModuleScope side-by-side (unusual but legal) —
    ``./index.js`` finds the str, bare ``"index.js"`` finds the
    ModuleScope.

    Each scope is a closed resolver namespace. Code inside the scope
    can reach only what the scope's dict declares. A dependency is
    not inherited; share it by spreading into each scope that needs
    it. See §4 for the full resolver semantics.

    Examples:
        # Multi-file scope with a subdirectory
        ModuleScope({"my-lib": ModuleScope({
            "index.js": "export { foo } from './lib/util.js';",
            "lib/util.js": "export function foo() { return 1; }",
        })})

        # Scope with a named dependency (bare import)
        ModuleScope({"my-lib": ModuleScope({
            "@peer": ModuleScope({"index.js": "export const P = 1;"}),
            "index.js": 'import { P } from "@peer"; export const x = P;',
        })})

        # Recursive: @app carries @agent/utils
        stdlib = ModuleScope({
            "@agent/utils": ModuleScope({
                "index.js": "export function slugify(s) {...}",
            }),
        })
        main = ModuleScope({
            **stdlib.modules,
            "@agent/fs": ModuleScope({
                **stdlib.modules,   # fs carries its own copy of utils
                "index.js": 'import { slugify } from "@agent/utils"; ...',
            }),
        })

        # Pure-dependency root (only ModuleScope entries, no str).
        # This is the common shape of what you hand to ctx.install.
        ModuleScope({
            "@agent/utils": ModuleScope({"index.js": "..."}),
            "@agent/fs": ModuleScope({"index.js": "..."}),
        })
    """
    modules: dict[str, str | ModuleScope]
```

**Validation at construction:**

- A ModuleScope that contains at least one `str` value must include an `"index.js"` entry — that's what a bare `import "<scope-name>"` resolves to from the parent scope. A scope without an `index.js` can't be imported by name.
- Scopes that contain only `ModuleScope` values (pure dependency containers) don't need `index.js`. They aren't themselves importable targets — they're registry wrappers. This is how a root scope handed to `ctx.install` typically looks.
- `str` keys may contain `/` (POSIX-style path within the scope's file tree): `"lib/util.js"`, `"tests/deep/nested.js"`. Path normalization is purely a resolver concern; the dict key stays as-given.
- Any key, at any depth, must not start with `./` or `../`. Those are relative specifiers used in JS import statements, never valid as dict keys.
- Nested `ModuleScope` values may themselves contain `str` and/or `ModuleScope` children. Nesting is recursive and unbounded — whatever the dependency graph requires.

```python
# Valid — scope with a single file
ModuleScope({"@agent/fs": ModuleScope({"index.js": "export default 1;"})})

# Valid — scope with subdirectory-style paths as str keys
ModuleScope({"my-lib": ModuleScope({
    "index.js": "export { foo } from './lib/util.js';",
    "lib/util.js": "export function foo() { return 1; }",
    "lib/helpers/str.js": "export function lower(s) { return s.toLowerCase(); }",
})})

# Valid — recursive nesting with a dep that itself carries a dep
ModuleScope({
    "@agent/utils": ModuleScope({"index.js": "export const U = 1;"}),
    "@agent/fs": ModuleScope({
        "@agent/utils": ModuleScope({"index.js": "export const U = 1;"}),
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

# Valid — pure-dependency container (only ModuleScope values → no
#         index.js required). Common shape for the root scope
#         passed to ctx.install.
ModuleScope({
    "@agent/utils": ModuleScope({"index.js": "export const U = 1;"}),
    "@agent/fs": ModuleScope({"index.js": "export const F = 2;"}),
})

# Invalid — scope has str entries but no index.js
ModuleScope({"@agent/fs": ModuleScope({"helpers.js": "..."})})  # ValueError

# Invalid — key starts with ./
ModuleScope({"./local": "..."})  # ValueError

# Invalid — value is neither str nor ModuleScope
ModuleScope({"@agent/fs": 42})  # TypeError
```

A single-file module (`ModuleScope({"lodash": "export const x = 1;"})`) from earlier drafts no longer parses. `lodash` as a str at root would be a file — but the root scope has no `index.js` sibling, so validation fails. Wrap the source in a ModuleScope (`ModuleScope({"lodash": ModuleScope({"index.js": "export const x = 1;"})})`) to declare a single-file dep. The tradeoff keeps the two-namespaces rule clean — str is always a file, ModuleScope is always a dep.

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

Each scope is a closed resolver namespace with two distinct sub-namespaces:

1. **Files** — `str` entries. Addressed by relative specifiers (`./`, `../`). Keys are POSIX-style paths; resolution uses `posixpath.normpath` against the referrer's directory within the scope. Paths that normalize past the scope root are errors.
2. **Dependencies** — `ModuleScope` entries. Addressed by bare specifiers. No path traversal — match the key exactly.

Relative specifiers never match a dependency. Bare specifiers never match a file. The value type of the dict entry is the namespace gate.

### The rule

Given `import "X" from referrer "Y"`:

1. Identify the scope `S` that contains `Y`. For a file `"path/to/foo.js"` within scope `Sname`, the containing scope is `Sname` and the referrer's position within it is `path/to/foo.js`. For `"<eval>"`, the containing scope is the root scope installed via `ctx.install` and the position is the root (empty path).
2. If `X` starts with `./` or `../`:
   - Compute `normalized = posixpath.normpath(posixpath.dirname(position_in_S) + "/" + X)`.
   - If `normalized` starts with `"../"` or equals `".."` → **error**: the specifier escapes scope `S`.
   - Otherwise look up `normalized` as a `str` key in `S`'s dict.
     - Found → canonical name is `"{canonical_scope_path}/{normalized}"`.
     - Not found, or found as `ModuleScope` → **error**: module not found.
3. If `X` is bare (no leading `./` or `../`):
   - Look up `X` as a `ModuleScope` key in `S`'s dict.
     - Found → canonical name is `"{canonical_scope_path}/{X}/index.js"` (X's entry point).
     - Not found, or found as `str` → **error**: module not found.

`canonical_scope_path` is the joined scope names from root down to `S` (the root's canonical path is empty). So a file at `lib/util.js` inside `@app/service` inside the root has canonical name `"@app/service/lib/util.js"`.

### Example: POSIX traversal within a scope

```python
app = ModuleScope({
    "@agent/utils": ModuleScope({"index.js": "export const U = 1;"}),
    "index.js": "export const foo = 'bar';",
    "lib/helpers.js": "export function help() { return 'helping'; }",
    "lib/utils/strings.js": "export function lower(s) { return s.toLowerCase(); }",
    "lib/index.js": """
        import { lower } from "./utils/strings.js";   // → "lib/utils/strings.js"
        import { help } from "./helpers.js";           // → "lib/helpers.js"
        import { foo } from "../index.js";             // → "index.js"
        export { lower, help, foo };
    """,
    "tests/test_lib.js": """
        import { lower } from "../lib/utils/strings.js";  // → "lib/utils/strings.js"
        import { foo } from "../index.js";                 // → "index.js"
        import { slugify } from "@agent/utils";            // bare → ModuleScope entry
    """,
    "tests/deep/nested.js": """
        import { foo } from "../../index.js";              // → "index.js"
        import { x } from "../../../escape.js";            // ERROR — past scope root
    """,
})
```

All paths are resolved by `posixpath.normpath`. The scope root is the ceiling; any normalized path starting with `../` is an escape attempt and rejects.

### Self-containment

The resolver only consults `S`'s own dict. No fallback to outer scopes, no inheritance from parents, no sibling visibility. Code in `@agent/fs/index.js` that imports `"@agent/utils"` looks up `"@agent/utils"` in `@agent/fs`'s dict — not in the root, not in any ancestor. If `@agent/fs` doesn't carry `@agent/utils`, the import fails even if the outer scope does carry it.

To share a dependency across scopes, spread it into each scope that needs it:

```python
utils = {"@agent/utils": ModuleScope({"index.js": "export const U = 1;"})}

main = ModuleScope({
    **utils,
    "@agent/fs": ModuleScope({
        **utils,
        "index.js": "import { U } from '@agent/utils'; ...",
    }),
    "@agent/http": ModuleScope({
        **utils,
        "index.js": "import { U } from '@agent/utils'; ...",
    }),
})
```

The same source gets registered under multiple canonical paths — `"@agent/utils/index.js"`, `"@agent/fs/@agent/utils/index.js"`, `"@agent/http/@agent/utils/index.js"`. QuickJS caches each canonical path independently; the three instances don't collide because the cache key is the canonical path, not the source.

### Resolution flowchart

```
import "X" from "Y" where Y lives at position P within scope S:

  Does X start with "./" or "../"?
  ├── Yes — relative specifier: file lookup with POSIX normalization
  │   ├── normalized = posixpath.normpath(dirname(P) + "/" + X)
  │   │   ├── normalized starts with "../" → Error: escape past scope root
  │   │   └── else:
  │   │       └── Look up normalized in S's str entries
  │   │           ├── Found → canonical: "{S-canonical-path}/{normalized}"
  │   │           └── Not found → Error: module not found
  │   └── (normalized never matches a ModuleScope entry — files only)
  │
  └── No — bare specifier: dependency lookup, exact match
      └── Look up X in S's ModuleScope entries
          ├── Found → canonical: "{S-canonical-path}/{X}/index.js"
          └── Not found → Error: module not found
      (X never matches a str entry — deps only)
```

`"<eval>"` is the referrer for code passed to `ctx.eval_async(module=True)`. Its containing scope is the root scope installed via `ctx.install`; its position within that scope is the empty string (the root directory). So `./foo.js` from eval normalizes to `foo.js` and looks for a root-level str entry; `@agent/fs` from eval looks for a root-level ModuleScope entry; `../anything` fails (escape past root).

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

* Each `str` entry under a canonical path that concatenates the containing scope's canonical path plus the entry's key (which may itself contain `/` if the user used a POSIX path): `"scope1/scope2/.../lib/util.js"`.
* Each `ModuleScope` entry as a scope record whose canonical path is the joined scope-path. Scope records also store, for each direct dict key, its kind (`File` or `Scope`) and — for `File` entries — the full POSIX path (so the resolver can do `normpath` against `str` entries only).

The root scope (the one passed to `ctx.install`) has the empty canonical path `""`. A file `"lib/util.js"` inside `@agent/fs` shows up as `"@agent/fs/lib/util.js"`. A file inside `@agent/fs/@peer` shows up as `"@agent/fs/@peer/index.js"`.

```rust
/// Tree registry populated from Python's ModuleScope at install time.
/// Flattened into HashMaps keyed on canonical paths (joined with '/');
/// the root scope has the empty path "".
struct FlatModuleStore {
    /// canonical_file_path → JS source.
    ///   Example keys (note: str entry keys can contain '/'):
    ///     "@agent/fs/index.js"                (file in root-level scope)
    ///     "@agent/fs/lib/util.js"             ("lib/util.js" str entry)
    ///     "@agent/fs/@peer/index.js"          (file in nested-dep scope)
    sources: HashMap<String, String>,

    /// canonical_scope_path → per-scope record describing the scope's
    /// direct dict entries. The resolver needs two views of each
    /// scope's contents:
    ///   * `files`: the set of str-entry keys (POSIX paths). A ./X
    ///     or ../X import normpaths against this set.
    ///   * `scopes`: the set of ModuleScope-entry keys. A bare X
    ///     import matches against this set only.
    scopes: HashMap<String, ScopeRecord>,
}

struct ScopeRecord {
    files: HashSet<String>,   // str-entry keys (may contain '/')
    scopes: HashSet<String>,  // ModuleScope-entry keys (bare specifiers)
}

impl Resolver for FlatModuleStore {
    /// `base` is the referrer's canonical path (a file path like
    /// "@agent/fs/lib/util.js" or "<eval>" for top-level eval).
    /// `name` is the import specifier as written in the source.
    fn resolve<'js>(&mut self, _ctx: &Ctx<'js>, base: &str, name: &str) -> Result<String> {
        // Identify the referrer's containing scope and its position
        // (path within that scope). Position is used as the
        // dirname-anchor for relative-specifier normalization.
        //
        // For "<eval>": containing scope = root (""), position = "".
        // For a file canonical path "A/B/path/to/foo.js" where "A/B"
        // is the containing scope's path: position = "path/to/foo.js".
        //
        // The install machinery stored, for each file in `sources`,
        // both its containing-scope canonical path and its within-
        // scope position, so we can look them up directly rather
        // than guessing a split (scope paths AND str keys both may
        // contain '/', so rsplit_once('/') is ambiguous).
        let (containing_scope, position_in_scope) = self.locate_referrer(base)?;

        let scope_rec = self.scopes
            .get(&containing_scope)
            .ok_or_else(|| Error::new_resolving(base, name))?;

        if name.starts_with("./") || name.starts_with("../") {
            // Relative specifier: POSIX normpath against the str
            // entries of the containing scope. The scope root is
            // the ceiling.
            let dir = posix_dirname(&position_in_scope);   // "" for eval, "lib" for "lib/foo.js"
            let normalized = posix_normpath(&format!("{}/{}", dir, name));
            if normalized.starts_with("../") || normalized == ".." {
                return Err(Error::new_resolving_message(
                    base, name, "relative import escapes module scope root",
                ));
            }
            if scope_rec.files.contains(&normalized) {
                Ok(join_path(&containing_scope, &normalized))
            } else {
                // Not in files. If it happens to match a ModuleScope
                // key, that's still an error — relative specifiers
                // never reach the ModuleScope namespace.
                Err(Error::new_resolving(base, name))
            }
        } else {
            // Bare specifier: exact match against ModuleScope
            // entries only. Never matches a str entry.
            if scope_rec.scopes.contains(name) {
                let dep_scope = join_path(&containing_scope, name);
                Ok(format!("{}/index.js", dep_scope))
            } else {
                Err(Error::new_resolving(base, name))
            }
        }
    }
}

/// `join_path("", "x") == "x"`, `join_path("a/b", "c") == "a/b/c"`.
fn join_path(scope: &str, child: &str) -> String {
    if scope.is_empty() { child.to_string() } else { format!("{}/{}", scope, child) }
}

/// Python-equivalent posixpath.normpath, restricted to the forms
/// we actually encounter (forward slashes, no leading slash on
/// within-scope paths). Collapses `./`, resolves `..`, preserves
/// leading `..` overflows (so the resolver can detect escape).
fn posix_normpath(p: &str) -> String { /* ... */ }

/// posixpath.dirname — "lib/foo.js" → "lib", "foo.js" → "", "" → "".
fn posix_dirname(p: &str) -> &str { /* ... */ }

impl Loader for FlatModuleStore {
    fn load<'js>(&mut self, ctx: &Ctx<'js>, name: &str) -> Result<Module<'js>> {
        let source = self.sources.get(name)
            .ok_or_else(|| Error::new_loading(name))?
            .clone();
        Module::declare(ctx.clone(), name, source)
    }
}
```

The backing store construction (from `Context.install`) walks the `ModuleScope` recursively. At each scope, Python declares the scope's direct entries with their kinds; for each `str` entry it then calls `add_source` with the canonical path (joined scope path + the str key as-given, which may contain `/`):

```python
def _install_scope(self, scope: ModuleScope, scope_path: str) -> None:
    # Declare the scope's membership. Two lists, one per kind, so the
    # Rust side can populate ScopeRecord.files vs ScopeRecord.scopes
    # without re-inspecting values.
    file_keys = [k for k, v in scope.modules.items() if isinstance(v, str)]
    dep_keys  = [k for k, v in scope.modules.items() if isinstance(v, ModuleScope)]
    self._runtime._engine_rt.declare_scope(scope_path, file_keys, dep_keys)

    # Register each str entry's source at its canonical path. The
    # key may contain '/', which becomes part of the canonical path
    # (e.g. "lib/util.js" inside "@agent/fs" → "@agent/fs/lib/util.js").
    for key, value in scope.modules.items():
        child_path = key if scope_path == "" else f"{scope_path}/{key}"
        if isinstance(value, str):
            self._runtime._engine_rt.add_source(
                child_path, value,
                containing_scope=scope_path, position_in_scope=key,
            )
        else:  # ModuleScope
            self._install_scope(value, child_path)

def install(self, scope: ModuleScope) -> None:
    self._install_scope(scope, "")   # root scope has empty path
```

`add_source` takes the canonical path plus the (containing_scope, position_in_scope) pair so the resolver can do `locate_referrer(base)` in O(1) — scope paths AND str keys both may contain `/`, so a plain rsplit of the canonical path is ambiguous.

### 5.3 New QjsRuntime / QjsContext methods

The backing `FlatModuleStore` lives on the runtime (§11 open-decision 4 — `set_loader` is per-runtime in rquickjs). Two methods on `QjsRuntime` mutate the store at install time; one method on `QjsContext` evaluates code as an ES module.

```rust
#[pymethods]
impl QjsRuntime {
    /// Register a file at `canonical_path`. `canonical_path` is the
    /// joined containing scope path + the str-entry key as-given
    /// (which may contain '/', e.g. "@agent/fs/lib/util.js" for a
    /// "lib/util.js" str key inside "@agent/fs"). `containing_scope`
    /// is the canonical path of the scope that directly holds this
    /// entry; `position_in_scope` is the str key itself. The pair
    /// is stored so the resolver can locate a referrer by canonical
    /// path without trying to split a path that contains '/' in
    /// both the scope portion and the key portion.
    fn add_source(
        &self,
        canonical_path: &str,
        source: &str,
        containing_scope: &str,
        position_in_scope: &str,
    ) -> PyResult<()>;

    /// Declare a scope's direct entries, separated by kind. `files`
    /// are the str-entry keys (may contain '/'); `scopes` are the
    /// ModuleScope-entry keys (bare specifiers). Called for each
    /// ModuleScope (including the root) at install time.
    fn declare_scope(
        &self,
        scope_path: &str,
        files: Vec<String>,
        scopes: Vec<String>,
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

See §5.2 for the Python `Context.install()` recursion that drives these two methods.

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
  → Rust: FlatModuleStore.sources and .scopes get populated with
    canonical-path keys. Each ScopeRecord tracks two sets —
    {files} and {scopes} — so the resolver can route relative
    vs bare specifiers to the correct sub-namespace.

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
| Relative specifier normalizes past scope root | `JSError(name="Error", message="relative import escapes module scope root")` |
| Relative specifier points at a ModuleScope entry (wrong namespace) | `JSError(name="Error", message="Could not load module '...'")` |
| Bare specifier points at a str entry (wrong namespace) | `JSError(name="Error", message="Could not load module '...'")` |
| Module evaluation throws | `JSError` (or `TimeoutError`, `MemoryLimitError` — same classification as eval) |
| Circular import | Not an error — ES modules handle cycles via live bindings |
| Re-install after import | Not an error — silently ignored (QuickJS module cache takes precedence) |

## 9. Testing

### 9.1 New test file

`tests/test_modules.py`:

**Registration and basic import:**
- Single-file module wrapped in a ModuleScope: `ModuleScope({"@agent/config": ModuleScope({"index.js": "..."})})`, import a constant via bare specifier.
- Multi-file scope: register scope, import from index.js.
- Internal imports: index.js imports ./helpers.js within scope.
- Transitive internal imports: a.js → ./b.js → ./c.js within scope.
- Pure-dependency root: root ModuleScope contains only nested scopes (no str entries, no index.js at root); eval imports one of the scopes via bare specifier.

**Resolver — scope-local lookup (§4):**
- Bare specifier resolves only within the containing scope's ModuleScope entries — not in the outer scope, not in a sibling. A scope that imports a dependency must carry it in its own dict.
- `./X` in scope A resolves to A's own `X.js` (a str entry); not to B's, even if B has an `X.js`.
- `./X` from top-level eval: works when the root scope has `X.js` as a str entry; errors when the root is a pure-dependency container (no file at root called `X.js`).
- Bare specifier matches ONLY ModuleScope entries. A bare specifier whose name happens to equal a str-entry key in the containing scope still errors (wrong namespace).
- Relative specifier matches ONLY str entries. `./name` where `name` is a ModuleScope-entry key still errors (wrong namespace).
- Two-namespace coexistence: a scope may legally have `"index.js"` as a str AND `"index.js"` as a ModuleScope (same key, different value types); `./index.js` finds the str, bare `index.js` finds the ModuleScope.
- A scope that uses a bare specifier for a dep it doesn't carry: import errors at eval time, even if an outer/parent/sibling scope does carry that dep.

**POSIX paths within scopes (§3.1 / §4):**
- Subdirectory str keys: `"lib/util.js"`, `"lib/helpers/str.js"` — index.js imports `"./lib/util.js"` → resolves to the `"lib/util.js"` str entry.
- `..` traversal within a scope: `"lib/index.js"` imports `"../index.js"` → normalizes to `"index.js"` at scope root → works.
- Normalization collapses `./` and `..`: `"./lib/../index.js"` → `"index.js"`; `"lib/./util.js"` → `"lib/util.js"`.
- `..` that escapes the scope root is an error: `"lib/foo.js"` imports `"../../escape.js"` → normalized starts with `"../"` → ResolveError.
- From top-level eval (position is scope root), `"../anything"` is always escape-past-root.
- Relative imports from a file deep in the scope normalize against the file's directory, not against the scope root.

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
- `../` that normalizes past scope root → JSError.
- `./X` specifier where `X` is a ModuleScope key (wrong namespace — relatives are files only) → JSError.
- Bare specifier `X` where `X` is a str key (wrong namespace — bares are deps only) → JSError.
- Scope with str entries missing `index.js` → ValueError at ModuleScope construction.
- Key starts with `./` or `../` → ValueError at construction (reserved for import specifiers).

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
