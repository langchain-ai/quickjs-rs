//! ES module cache, resolver, and loader.
//!
//! rquickjs owns resolver/loader instances at the runtime level, and
//! module state here is runtime-owned as well.
//!
//! Resolution is dynamic-handler driven:
//!   * relative specifiers: compute one normalized requested key from
//!     `(base, name)` and ask the handler.
//!   * bare specifiers: pass the specifier through unchanged and ask
//!     the handler.
//!
//! The handler can return:
//!   * `None` -> miss
//!   * `str` -> source for the provided requested key
//!
//! Returned source is cached by resolved key so `StoreLoader` can
//! satisfy rquickjs's later load callback.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use pyo3::prelude::*;
use pyo3::types::PyAny;
use rquickjs::loader::{Loader, Resolver};
use rquickjs::module::Declared;
use rquickjs::{Ctx, Error, Module, Result as QjsResult};

pub(crate) struct ModuleStore {
    /// Resolver/loader handoff cache: resolved module key -> source text.
    pub resolved_sources: HashMap<String, String>,
    /// Optional Python dynamic import callback.
    pub dynamic_source_handler: Option<Py<PyAny>>,
}

impl Default for ModuleStore {
    fn default() -> Self {
        Self {
            resolved_sources: HashMap::new(),
            dynamic_source_handler: None,
        }
    }
}

#[derive(Clone)]
pub(crate) struct StoreHandle {
    // Shared runtime-owned module state accessed by both resolver and
    // loader callbacks. Interior mutability keeps the callback API
    // simple while still allowing resolve() to insert newly fetched
    // source text.
    inner: Rc<RefCell<ModuleStore>>,
}

impl Default for StoreHandle {
    fn default() -> Self {
        Self {
            inner: Rc::new(RefCell::new(ModuleStore::default())),
        }
    }
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

    fn with_ref<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&ModuleStore) -> R,
    {
        let store = self.inner.borrow();
        f(&store)
    }

    pub(crate) fn set_source_handler(&self, handler: Option<Py<PyAny>>) {
        self.with_mut(|store| {
            // Replace runtime-level dynamic handler atomically.
            store.dynamic_source_handler = handler;
        });
    }

    pub(crate) fn add_source(&self, key: &str, source: &str) {
        self.with_mut(|store| {
            // Last-write wins: resolver can refresh canonical source by key.
            store
                .resolved_sources
                .insert(key.to_string(), source.to_string());
        });
    }

    fn has_source(&self, key: &str) -> bool {
        self.with_ref(|store| store.resolved_sources.contains_key(key))
    }

    #[allow(clippy::enum_variant_names)]
    fn request_dynamic_source(
        &self,
        requested_key: &str,
        referrer: Option<&str>,
        specifier: &str,
    ) -> Result<DynamicLookupResult, String> {
        Python::attach(|py| {
            // Clone the callable under the GIL before releasing the
            // store borrow; this avoids holding RefCell borrows across
            // arbitrary Python execution.
            let handler = self.with_ref(|store| {
                store
                    .dynamic_source_handler
                    .as_ref()
                    .map(|callback| callback.clone_ref(py))
            });
            let Some(handler) = handler else {
                return Ok(DynamicLookupResult::Unavailable);
            };

            let result = handler
                .bind(py)
                .call1((requested_key, referrer, specifier))
                .map_err(|e| {
                    let referrer = referrer.unwrap_or("None");
                    format!(
                        "import handler failed for {requested_key} \\
(referrer={referrer}, specifier={specifier}): {e}"
                    )
                })?;

            if result.is_none() {
                return Ok(DynamicLookupResult::Miss);
            }

            if let Ok(source) = result.extract::<String>() {
                return Ok(DynamicLookupResult::Source { source });
            }

            Err(format!(
                "import handler must return str | None for {requested_key}"
            ))
        })
    }
}

enum DynamicLookupResult {
    // Caller must cache source before loader runs.
    Source { source: String },
    // Handler ran but declined this candidate.
    Miss,
    // No handler configured at runtime.
    Unavailable,
}

/// Newtype so we can `impl Resolver` without violating the orphan
/// rule.
pub(crate) struct StoreResolver(pub StoreHandle);

impl Resolver for StoreResolver {
    fn resolve<'js>(&mut self, _ctx: &Ctx<'js>, base: &str, name: &str) -> QjsResult<String> {
        resolve_with(&self.0, base, name)
    }
}

/// Matching newtype for the loader half.
pub(crate) struct StoreLoader(pub StoreHandle);

impl Loader for StoreLoader {
    fn load<'js>(&mut self, ctx: &Ctx<'js>, name: &str) -> QjsResult<Module<'js, Declared>> {
        // Resolver is responsible for pre-populating `resolved_sources`; loader
        // only materializes already-cached modules.
        let source = self
            .0
            .with_ref(|store| store.resolved_sources.get(name).cloned());
        let Some(source) = source else {
            return Err(Error::new_loading(name));
        };
        Module::declare(ctx.clone(), name.to_string(), source)
    }
}

fn resolve_with(store_handle: &StoreHandle, base: &str, name: &str) -> QjsResult<String> {
    let requested_key = if name.starts_with("./") || name.starts_with("../") {
        resolve_relative_candidate(base, name)?
    } else {
        name.to_string()
    };

    if store_handle.has_source(&requested_key) {
        return Ok(requested_key);
    }

    let lookup = store_handle
        .request_dynamic_source(&requested_key, handler_referrer(base), name)
        .map_err(|msg| Error::new_resolving_message(base, name, &msg))?;
    let source = match lookup {
        DynamicLookupResult::Source { source } => source,
        DynamicLookupResult::Miss | DynamicLookupResult::Unavailable => {
            return Err(Error::new_resolving(base, name));
        }
    };
    let stripped = maybe_strip_ts(&requested_key, &source)
        .map_err(|msg| Error::new_resolving_message(base, name, &msg))?;
    store_handle.add_source(&requested_key, &stripped);
    Ok(requested_key)
}

fn handler_referrer(base: &str) -> Option<&str> {
    if base == "<eval>" {
        None
    } else {
        Some(base)
    }
}

fn resolve_relative_candidate(base: &str, name: &str) -> QjsResult<String> {
    // Top-level eval has no on-disk parent path; treat as scope root.
    let anchor = if base == "<eval>" {
        ""
    } else {
        posix_dirname(base)
    };
    let joined = if anchor.is_empty() {
        name.to_string()
    } else {
        format!("{}/{}", anchor, name)
    };
    match normalize_path(&joined) {
        Some(key) => Ok(key),
        None => Err(Error::new_resolving_message(
            base,
            name,
            "relative import escapes module scope root",
        )),
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

/// POSIX-path normalizer restricted to forward-slash paths with
/// relative segments. Returns None if the path walks above root.
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

/// Transparent TypeScript stripping. If the module key
/// ends in `.ts` / `.mts` / `.cts` / `.tsx`, run the source through
/// oxidase. Other extensions pass through unchanged.
pub(crate) fn maybe_strip_ts(key: &str, source: &str) -> Result<String, String> {
    let source_type = if key.ends_with(".ts") || key.ends_with(".mts") || key.ends_with(".cts") {
        oxidase::SourceType::ts()
    } else if key.ends_with(".tsx") {
        oxidase::SourceType::tsx()
    } else {
        return Ok(source.to_string());
    };

    // Keep panics from crossing FFI boundaries into Python callers.
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
        assert_eq!(normalize_path(".."), None);
        assert_eq!(normalize_path("../foo.js"), None);
        assert_eq!(normalize_path("lib/../../foo.js"), None);
    }
}
