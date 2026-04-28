//! ES module registry, resolver, and loader.
//!
//! Populated from Python's ModuleScope at install time via
//! `Context.install()` → recursive walk → `add_source` /
//! `register_subscope`. Queried by rquickjs via the Resolver +
//! Loader traits when a module-mode eval hits an `import` statement.
//!
//! Storage shape:
//!
//!   * `sources: HashMap<String, String>` — canonical module path
//!     → JS source. Canonical paths are built from the containing
//!     scope's canonical path plus the str-entry key (the key may
//!     itself contain `/` if the user used a POSIX subpath).
//!     Examples:
//!       "@agent/config/index.js"            (file in root-level dep)
//!       "@agent/fs/lib/helpers.js"          ("lib/helpers.js" str)
//!       "@agent/app/@peer/index.js"         (file in nested-dep scope)
//!
//!   * `scopes: HashMap<String, ScopeEntry>` — canonical scope path
//!     → {files, subscopes}. `files` is the set of str-valued
//!     child keys (may contain '/'); `subscopes` is the set of
//!     ModuleScope-valued child keys (bare specifiers). The root
//!     scope uses the empty canonical path.
//!
//! Resolver rule: identify the referrer's containing scope by
//! prefix match against the registered scope paths (longest wins),
//! then route based on specifier shape:
//!
//!   * `./X` or `../X` — POSIX-normalize against the referrer's
//!     position within the scope; look the normalized path up in
//!     `files`. Normalizing past the scope root is an error.
//!   * bare `X` — exact match against `subscopes`; resolve to
//!     `{scope}/{X}/index.js`.
//!
//! The store is per-runtime. rquickjs's `set_loader` takes ownership
//! of the Resolver and Loader, so we wrap the store in an
//! `Rc<RefCell<...>>` and hand two newtypes (`StoreResolver`,
//! `StoreLoader`) that both carry a clone of the handle. The
//! runtime keeps its own clone so Python-side `install()` calls
//! can keep writing into the same backing store.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use rquickjs::loader::{Loader, Resolver};
use rquickjs::module::Declared;
use rquickjs::{Ctx, Error, Module, Result as QjsResult};

#[derive(Default)]
pub(crate) struct ScopeEntry {
    /// str-valued child keys of this scope (POSIX paths, may
    /// contain '/'). Addressable only by ./X or ../X specifiers.
    pub files: HashSet<String>,
    /// ModuleScope-valued child keys of this scope (bare import
    /// names). Addressable only by bare specifiers.
    pub subscopes: HashSet<String>,
}

#[derive(Default)]
pub(crate) struct ModuleStore {
    /// canonical_path → JS source. See file-header examples.
    pub sources: HashMap<String, String>,
    /// canonical_scope_path → membership. The root scope lives
    /// under the empty key. Any canonical path appearing in
    /// `sources` is inside exactly one of these scopes.
    pub scopes: HashMap<String, ScopeEntry>,
}

impl ModuleStore {
    /// Ensure a scope entry exists for `scope_path`. Called by
    /// both `add_source` (for the file's containing scope) and
    /// `register_subscope` (for the parent that holds the sub).
    /// Cheap: a fresh ScopeEntry is two empty sets.
    fn ensure_scope(&mut self, scope_path: &str) -> &mut ScopeEntry {
        self.scopes
            .entry(scope_path.to_string())
            .or_insert_with(ScopeEntry::default)
    }

    pub(crate) fn add_source(
        &mut self,
        scope_path: &str,
        key: &str,
        canonical_path: &str,
        source: &str,
    ) {
        self.ensure_scope(scope_path).files.insert(key.to_string());
        self.sources
            .insert(canonical_path.to_string(), source.to_string());
    }

    pub(crate) fn register_subscope(&mut self, scope_path: &str, child_key: &str) {
        self.ensure_scope(scope_path)
            .subscopes
            .insert(child_key.to_string());
        // Also record the child itself as a known scope so a file
        // inside it can be located via locate_containing_scope.
        // Its ScopeEntry stays empty until its own files/subscopes
        // arrive.
        let child_path = join_path(scope_path, child_key);
        self.scopes
            .entry(child_path)
            .or_insert_with(ScopeEntry::default);
    }

    /// Given a referrer canonical path (a file path like
    /// "@agent/fs/lib/helpers.js" or "<eval>"), find which scope
    /// it belongs to. Returns (scope_path, position_within_scope).
    ///
    /// <eval> is the special referrer for top-level module eval;
    /// its containing scope is the root ("") and its position is
    /// the scope root (""). A file whose canonical path exactly
    /// equals a known scope path shouldn't happen (we only record
    /// str entries in `sources`, and they always sit INSIDE a
    /// scope), but we handle it defensively as "scope with empty
    /// position".
    ///
    /// Longest-prefix match: given scope paths "@agent" and
    /// "@agent/fs", a referrer under "@agent/fs/..." belongs to
    /// "@agent/fs", not "@agent". Iteration is O(n_scopes) which
    /// is fine for any realistic tree — install is rare, resolve
    /// is the hot path but `n_scopes` tops out at a few dozen.
    fn locate_containing_scope(&self, referrer: &str) -> Option<(String, String)> {
        if referrer == "<eval>" {
            // Caller should have set up the root scope via the
            // first install() call; if there's no root yet, let
            // the caller surface the error.
            return Some((String::new(), String::new()));
        }
        let mut best: Option<(&str, usize)> = None;
        for scope_path in self.scopes.keys() {
            if scope_path.is_empty() {
                // Root scope: everything is inside it, but a more
                // specific scope beats the root. Track as fallback.
                if best.is_none() {
                    best = Some((scope_path, 0));
                }
                continue;
            }
            let s = scope_path.as_str();
            if referrer == s {
                return Some((scope_path.clone(), String::new()));
            }
            // `referrer` is under `s` iff referrer starts with `s/`.
            // That leading-slash check avoids a false positive where
            // scope "@a" would claim a sibling referrer "@abc/x".
            if referrer.len() > s.len() + 1
                && referrer.starts_with(s)
                && referrer.as_bytes()[s.len()] == b'/'
            {
                let cand_len = s.len();
                match best {
                    Some((_, best_len)) if best_len >= cand_len => {}
                    _ => best = Some((s, cand_len)),
                }
            }
        }
        let (scope, len) = best?;
        let position = if len == 0 {
            // Root scope: position is the entire referrer.
            referrer.to_string()
        } else {
            // Strip "{scope}/" prefix.
            referrer[len + 1..].to_string()
        };
        Some((scope.to_string(), position))
    }
}

/// Shared handle to the per-runtime ModuleStore. The Python side
/// holds one clone (on QjsRuntime) for install writes; the
/// Resolver + Loader each hold a clone to service rquickjs callbacks.
#[derive(Clone, Default)]
pub(crate) struct StoreHandle {
    inner: Rc<RefCell<ModuleStore>>,
}

impl StoreHandle {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    pub(crate) fn with_mut<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&mut ModuleStore) -> R,
    {
        let mut store = self.inner.borrow_mut();
        f(&mut store)
    }
}

/// Newtype so we can `impl Resolver` without violating the orphan
/// rule — `StoreHandle` is local, and the trait requires owned
/// mutability anyway.
pub(crate) struct StoreResolver(pub StoreHandle);

impl Resolver for StoreResolver {
    fn resolve<'js>(&mut self, _ctx: &Ctx<'js>, base: &str, name: &str) -> QjsResult<String> {
        let store = self.0.inner.borrow();
        resolve_with(&store, base, name)
    }
}

/// Matching newtype for the loader half.
pub(crate) struct StoreLoader(pub StoreHandle);

impl Loader for StoreLoader {
    fn load<'js>(&mut self, ctx: &Ctx<'js>, name: &str) -> QjsResult<Module<'js, Declared>> {
        // Clone the source out of the store before calling into
        // Module::declare — `Module::declare` takes an owned
        // string, and keeping the borrow live would conflict with
        // any nested resolve() that fires during parse.
        let source = {
            let store = self.0.inner.borrow();
            match store.sources.get(name) {
                Some(s) => s.clone(),
                None => return Err(Error::new_loading(name)),
            }
        };
        Module::declare(ctx.clone(), name.to_string(), source)
    }
}

fn resolve_with(store: &ModuleStore, base: &str, name: &str) -> QjsResult<String> {
    let (scope_path, position) = store
        .locate_containing_scope(base)
        .ok_or_else(|| Error::new_resolving(base, name))?;

    let scope_entry = match store.scopes.get(&scope_path) {
        Some(e) => e,
        None => return Err(Error::new_resolving(base, name)),
    };

    if name.starts_with("./") || name.starts_with("../") {
        // Relative specifier — POSIX-normalize against the file set.
        let anchor = posix_dirname(&position);
        let joined = if anchor.is_empty() {
            // Trim the leading "./" off `name` if present so
            // "./foo.js" under anchor "" normalizes to "foo.js".
            name.to_string()
        } else {
            format!("{}/{}", anchor, name)
        };
        let normalized = match normalize_path(&joined) {
            Some(n) => n,
            None => {
                return Err(Error::new_resolving_message(
                    base,
                    name,
                    "relative import escapes module scope root",
                ))
            }
        };
        if scope_entry.files.contains(&normalized) {
            Ok(join_path(&scope_path, &normalized))
        } else {
            // Wrong namespace (relative can't reach a subscope) or
            // genuinely missing — both are resolve errors.
            Err(Error::new_resolving(base, name))
        }
    } else {
        // Bare specifier — match the subscope namespace only.
        if !scope_entry.subscopes.contains(name) {
            return Err(Error::new_resolving(base, name));
        }
        let dep_scope = join_path(&scope_path, name);
        // Pick the dep scope's actual index file. The dep's
        // ScopeEntry was populated as its own `install()` frames
        // recursed down — consult it now. The extension order
        // mirrors Python's `_INDEX_EXTENSIONS` in modules.py:
        // JS variants first (no strip), then TS, then JSX/TSX.
        //
        // If the dep scope is missing from `scopes` entirely, the
        // Python validation in ModuleScope.__post_init__ should
        // have caught it — but be defensive and fall through to
        // the default "index.js" canonical path so the loader's
        // own `Error::new_loading` surfaces a clearer message.
        let index_key = store
            .scopes
            .get(&dep_scope)
            .and_then(|dep_entry| pick_index_file(&dep_entry.files))
            .unwrap_or("index.js");
        Ok(format!("{}/{}", dep_scope, index_key))
    }
}

/// Pick a scope's entry-point file from its `files` set. Returns
/// the first `index.<ext>` match in preference order, or None if
/// none is present. Mirrors `_INDEX_EXTENSIONS` in modules.py.
fn pick_index_file(files: &HashSet<String>) -> Option<&'static str> {
    const CANDIDATES: &[&str] = &[
        "index.js",
        "index.mjs",
        "index.cjs",
        "index.ts",
        "index.mts",
        "index.cts",
        "index.jsx",
        "index.tsx",
    ];
    CANDIDATES.iter().copied().find(|c| files.contains(*c))
}

/// `join_path("", "x") == "x"`, `join_path("a/b", "c") == "a/b/c"`.
/// The root-scope special case keeps files at root (canonical names
/// with no leading slash) consistent with the containing-scope
/// convention elsewhere.
pub(crate) fn join_path(scope: &str, child: &str) -> String {
    if scope.is_empty() {
        child.to_string()
    } else {
        format!("{}/{}", scope, child)
    }
}

/// posixpath.dirname — "lib/foo.js" → "lib", "foo.js" → "",
/// "" → "".
fn posix_dirname(p: &str) -> &str {
    match p.rfind('/') {
        Some(i) => &p[..i],
        None => "",
    }
}

/// POSIX-path normalizer restricted to the forms we actually
/// encounter (forward slashes, relative paths with no leading
/// slash). Returns None if the path walks above the scope root —
/// the caller surfaces that as a resolve error.
///
/// Deliberately implemented by hand rather than via a crate:
///   * only a couple dozen lines
///   * avoids pulling in `pathdiff` / `path-slash` / `relative-path`
///   * the "starts with ../ after normalization" signal is the
///     escape-detection we need; std/path doesn't expose it that
///     way
pub(crate) fn normalize_path(path: &str) -> Option<String> {
    let mut parts: Vec<&str> = Vec::new();
    for segment in path.split('/') {
        match segment {
            "" | "." => {}
            ".." => {
                if parts.is_empty() {
                    return None;
                }
                parts.pop();
            }
            other => parts.push(other),
        }
    }
    Some(parts.join("/"))
}

/// Transparent TypeScript stripping. If the scope-entry key
/// ends in `.ts` or `.tsx`, run the source through oxidase to
/// erase type annotations (plus transform enums, namespaces, and
/// parameter properties — see the oxidase README). Any other
/// extension (`.js`, `.mjs`, `.cjs`, or none) passes through
/// unchanged.
///
/// On a parser panic, returns the aggregated error messages as a
/// `String`. On parser success with non-fatal diagnostics, ignores
/// them — oxidase is deliberately lenient (see its parser options:
/// `allow_return_outside_function: true, allow_skip_ambient: true`)
/// and the transpile output in those cases is still usable.
///
/// Import specifiers are NOT rewritten: `import { x } from
/// "./y.ts"` stays `"./y.ts"` in the stripped output. That's
/// important for our resolver — it looks up keys exactly as they
/// appear in the ModuleScope dict, so a `.ts` key must stay a
/// `.ts` specifier. Verified against oxidase 045ea46b by
/// inspecting `handle_import_declaration` — it only patches
/// type-only imports (`import type { ... }`).
pub(crate) fn maybe_strip_ts(key: &str, source: &str) -> Result<String, String> {
    let source_type = if key.ends_with(".ts") || key.ends_with(".mts") || key.ends_with(".cts") {
        oxidase::SourceType::ts()
    } else if key.ends_with(".tsx") {
        oxidase::SourceType::tsx()
    } else {
        // .js, .mjs, .cjs, no extension, anything else — pass
        // through. Users who want .ts behavior on an odd filename
        // can rename; we don't heuristically guess.
        return Ok(source.to_string());
    };

    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let allocator = oxidase::Allocator::default();
        let mut buf = source.to_string();
        let ret = oxidase::transpile(&allocator, source_type, &mut buf);
        (
            buf,
            ret.parser_panicked,
            ret.parser_errors
                .iter()
                .map(|d| d.to_string())
                .collect::<Vec<_>>(),
        )
    }));

    let (buf, panicked, errors) =
        outcome.map_err(|_| format!("oxidase panicked unexpectedly while parsing {}", key))?;

    if panicked {
        let msg = if errors.is_empty() {
            format!("oxidase failed to parse {}", key)
        } else {
            format!("TypeScript parse error in {}: {}", key, errors.join("; "))
        };
        return Err(msg);
    }

    Ok(buf)
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn normalize_basic() {
        assert_eq!(normalize_path("foo.js"), Some("foo.js".to_string()));
        assert_eq!(
            normalize_path("lib/util.js"),
            Some("lib/util.js".to_string())
        );
        assert_eq!(
            normalize_path("lib/./util.js"),
            Some("lib/util.js".to_string())
        );
        assert_eq!(normalize_path("lib/../foo.js"), Some("foo.js".to_string()));
        assert_eq!(
            normalize_path("lib/sub/../util.js"),
            Some("lib/util.js".to_string())
        );
    }

    #[test]
    fn normalize_escape_is_none() {
        // Reaching above the scope root returns None so the
        // resolver surfaces "relative import escapes module scope root".
        assert_eq!(normalize_path(".."), None);
        assert_eq!(normalize_path("../foo.js"), None);
        assert_eq!(normalize_path("lib/../../foo.js"), None);
    }

    #[test]
    fn locate_root_scope() {
        let mut s = ModuleStore::default();
        s.ensure_scope("");
        s.ensure_scope("@agent/fs");
        assert_eq!(
            s.locate_containing_scope("<eval>"),
            Some((String::new(), String::new()))
        );
        assert_eq!(
            s.locate_containing_scope("@agent/fs/index.js"),
            Some(("@agent/fs".to_string(), "index.js".to_string()))
        );
        assert_eq!(
            s.locate_containing_scope("@agent/fs/lib/util.js"),
            Some(("@agent/fs".to_string(), "lib/util.js".to_string()))
        );
    }

    #[test]
    fn locate_longest_scope_wins() {
        let mut s = ModuleStore::default();
        s.ensure_scope("@a");
        s.ensure_scope("@a/b");
        // Referrer under @a/b should match @a/b, not @a.
        assert_eq!(
            s.locate_containing_scope("@a/b/index.js"),
            Some(("@a/b".to_string(), "index.js".to_string()))
        );
    }

    #[test]
    fn locate_sibling_prefix_match_rejected() {
        // Ensure the "boundary on /" check defeats the "@a" ⊂ "@abc"
        // false prefix.
        let mut s = ModuleStore::default();
        s.ensure_scope("@a");
        s.ensure_scope("@abc");
        assert_eq!(
            s.locate_containing_scope("@abc/index.js"),
            Some(("@abc".to_string(), "index.js".to_string()))
        );
    }
}
