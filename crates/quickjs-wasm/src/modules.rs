//! ES module support — synchronous host-backed Resolver + Loader
//!
//! QuickJS resolves *static* imports synchronously at instantiation. The two
//! env imports below must be synchronous for exactly this reason (async would
//! fail on any module with static sub-imports). Both quickjs-wasi and the
//! -spec branch are synchronous for the same reason; we follow suit.
//!
//! ## Host imports
//!
//! ```text
//! env.host_module_normalize(base_ptr, base_len, spec_ptr, spec_len,
//!                           out_len_ptr) -> name_ptr
//! env.host_module_load(name_ptr, name_len, out_len_ptr) -> source_ptr
//! ```
//!
//! `host_module_normalize` resolves (base, specifier) → canonical name.
//! Returns a pointer into guest linear memory (written by the host into a
//! `qjs_alloc`-ed region) and writes the length to `*out_len_ptr`. Returns 0
//! on failure (unknown module). The guest reads the bytes, copies to a String,
//! then MUST call `qjs_free(ptr, len)` to release the host-allocated region.
//!
//! `host_module_load` takes a canonical name and returns a pointer to the
//! module source (UTF-8). Same ownership convention: guest reads, then frees.
//!
//! ## Resolve cache
//!
//! A per-context thread-local `HashMap<(base, specifier), canonical>` avoids
//! duplicate host round-trips on repeated static import edges (at-most-once per
//! edge, determinism by construction — -spec ADR-0003).
//!
//! ## eval_module export
//!
//! `eval_module(code_ptr, code_len, name_ptr, name_len, out_handle) -> status`
//! evaluates the given source as an ES module under `name`. Module::declare
//! compiles + links; the module is then evaluated and a handle to its namespace
//! is written to `*out_handle`. Async top-level-await inside a module follows
//! the same eval_async drive loop the host already knows.

use std::cell::RefCell;
use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};

use rquickjs::loader::{ImportAttributes, Loader, Resolver};
use rquickjs::module::Declared;
use rquickjs::{Ctx, Module, Result};

use crate::engine::{
    with_context, STATUS_BAD_INPUT, STATUS_JS_ERROR, STATUS_NO_ENGINE, STATUS_OK, STATUS_PANIC,
};
use crate::handles::{mint_value, NULL_HANDLE};
use crate::mem::read_input;

// ---------------------------------------------------------------------------
// Host imports (synchronous — QuickJS static-import linking requires it).
// ---------------------------------------------------------------------------

extern "C" {
    /// Resolve (base, specifier) -> canonical name written into guest memory.
    /// Returns a ptr to a `qjs_alloc`-ed region; writes the byte length to
    /// `*out_len`. Returns 0 on failure. The guest owns the allocation and
    /// must call `qjs_free(ptr, len)` after copying.
    fn host_module_normalize(
        base_ptr: *const u8,
        base_len: usize,
        spec_ptr: *const u8,
        spec_len: usize,
        out_len: *mut usize,
    ) -> *const u8;

    /// Load module source for a canonical name. Returns a ptr to a
    /// `qjs_alloc`-ed region of UTF-8 source bytes; writes byte length to
    /// `*out_len`. Returns 0 on failure. Guest must `qjs_free(ptr, len)` after
    /// use.
    fn host_module_load(name_ptr: *const u8, name_len: usize, out_len: *mut usize) -> *const u8;
}

// Upper bound on name/source sizes — prevents host-driven OOM.
const MAX_NAME_LEN: usize = 4096;
const MAX_SOURCE_LEN: usize = 16 * 1024 * 1024; // 16 MiB

// ---------------------------------------------------------------------------
// Resolve cache — at-most-once per (base, specifier) edge.
// ---------------------------------------------------------------------------

thread_local! {
    static RESOLVE_CACHE: RefCell<HashMap<(String, String), String>> =
        RefCell::new(HashMap::new());
}

fn cache_get(base: &str, spec: &str) -> Option<String> {
    RESOLVE_CACHE.with(|c| c.borrow().get(&(base.to_owned(), spec.to_owned())).cloned())
}

fn cache_set(base: &str, spec: &str, canonical: String) {
    RESOLVE_CACHE.with(|c| {
        c.borrow_mut()
            .insert((base.to_owned(), spec.to_owned()), canonical);
    });
}

// ---------------------------------------------------------------------------
// Call a host import that returns (ptr, len) into guest linear memory, reads
// the bytes into an owned String, and frees the allocation. Returns None if
// the host returns a null ptr, the length exceeds the cap, or the bytes aren't
// valid UTF-8.
// ---------------------------------------------------------------------------

unsafe fn read_host_string(ptr: *const u8, len: usize, max: usize) -> Option<String> {
    if ptr.is_null() || len == 0 || len > max {
        return None;
    }
    // SAFETY: ptr points into our own linear memory for `len` bytes, allocated
    // by the host via qjs_alloc. The slice is valid for the copy below; we
    // call qjs_free immediately after.
    let bytes = std::slice::from_raw_parts(ptr, len).to_vec();
    crate::mem::qjs_free_raw(ptr, len);
    String::from_utf8(bytes).ok()
}

// ---------------------------------------------------------------------------
// HostResolver: implements Resolver via host_module_normalize.
// ---------------------------------------------------------------------------

pub struct HostResolver;

impl Resolver for HostResolver {
    fn resolve<'js>(
        &mut self,
        _ctx: &Ctx<'js>,
        base: &str,
        name: &str,
        _attrs: Option<ImportAttributes<'js>>,
    ) -> Result<String> {
        // Cache hit: skip the host round-trip.
        if let Some(cached) = cache_get(base, name) {
            return Ok(cached);
        }

        let base_bytes = base.as_bytes();
        let name_bytes = name.as_bytes();
        let mut out_len: usize = 0;

        let canonical = unsafe {
            let ptr = host_module_normalize(
                base_bytes.as_ptr(),
                base_bytes.len(),
                name_bytes.as_ptr(),
                name_bytes.len(),
                &mut out_len,
            );
            read_host_string(ptr, out_len, MAX_NAME_LEN)
        };

        match canonical {
            Some(c) => {
                cache_set(base, name, c.clone());
                Ok(c)
            }
            None => Err(rquickjs::Error::new_resolving(base, name)),
        }
    }
}

// ---------------------------------------------------------------------------
// HostLoader: implements Loader via host_module_load.
// ---------------------------------------------------------------------------

pub struct HostLoader;

impl Loader for HostLoader {
    fn load<'js>(
        &mut self,
        ctx: &Ctx<'js>,
        name: &str,
        _attrs: Option<ImportAttributes<'js>>,
    ) -> Result<Module<'js, Declared>> {
        let name_bytes = name.as_bytes();
        let mut out_len: usize = 0;

        let source = unsafe {
            let ptr = host_module_load(name_bytes.as_ptr(), name_bytes.len(), &mut out_len);
            read_host_string(ptr, out_len, MAX_SOURCE_LEN)
        };

        let src = match source {
            Some(s) => s,
            None => return Err(rquickjs::Error::new_loading(name)),
        };
        // Transparent TypeScript stripping: a `.ts`/`.mts`/`.cts`/`.tsx`
        // canonical name is type-stripped before QuickJS sees it. Any
        // other extension passes through unchanged.
        let src = match maybe_strip_ts(name, src) {
            Ok(s) => s,
            // A TS parse error surfaces as a module-load failure → the eval
            // returns STATUS_JS_ERROR → host JSError.
            Err(_) => return Err(rquickjs::Error::new_loading(name)),
        };
        Module::declare(ctx.clone(), name, src)
    }
}

/// Transparent TypeScript stripping (mirrors the native crate's
/// `maybe_strip_ts`). If `name` ends in `.ts`/`.mts`/`.cts` → strip as TS,
/// `.tsx` → strip as TSX (erases type annotations; transforms enums,
/// namespaces, and parameter properties — see the oxidase README). Any other
/// extension passes through unchanged. No type checking.
///
/// Import specifiers are NOT rewritten: `import { x } from "./y.ts"` stays
/// `"./y.ts"` so the resolver still finds the key exactly as written.
///
/// `oxidase::transpile` can panic on malformed input, so it runs inside
/// `catch_unwind` — a parser panic becomes a clean `Err`, never an unwind
/// across the wasm boundary.
fn maybe_strip_ts(name: &str, source: String) -> core::result::Result<String, String> {
    let source_type = if name.ends_with(".ts") || name.ends_with(".mts") || name.ends_with(".cts") {
        oxidase::SourceType::ts()
    } else if name.ends_with(".tsx") {
        oxidase::SourceType::tsx()
    } else {
        return Ok(source); // .js/.mjs/.cjs/no-ext/anything else — pass through
    };

    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let allocator = oxidase::Allocator::default();
        let mut buf = source;
        let ret = oxidase::transpile(&allocator, source_type, &mut buf);
        let errors: Vec<String> = ret.parser_errors.iter().map(|d| d.to_string()).collect();
        (buf, ret.parser_panicked, errors)
    }));

    let (buf, panicked, errors) =
        outcome.map_err(|_| format!("oxidase panicked while parsing {name}"))?;
    if panicked {
        return Err(if errors.is_empty() {
            format!("oxidase failed to parse {name}")
        } else {
            format!("TypeScript parse error in {name}: {}", errors.join("; "))
        });
    }
    Ok(buf)
}

// ---------------------------------------------------------------------------
// eval_module export — compile + link + eval an ES module by source.
//
// Signature: eval_module(code_ptr, code_len, name_ptr, name_len, out_handle)
//            -> status
//
// `name` is the module's canonical identifier (used by QuickJS as the "base"
// for resolving its static imports). `out_handle` receives a handle to the
// module namespace object on success.
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn eval_module(
    code_ptr: *const u8,
    code_len: usize,
    name_ptr: *const u8,
    name_len: usize,
    out_handle: *mut i32,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        // Read source.
        let code_bytes = match read_input(code_ptr, code_len) {
            Some(b) => b,
            None => return STATUS_BAD_INPUT,
        };
        let source = match std::str::from_utf8(code_bytes) {
            Ok(s) => s,
            Err(_) => return STATUS_BAD_INPUT,
        };
        // Read module name (required for static-import resolution).
        let name_bytes = match read_input(name_ptr, name_len) {
            Some(b) => b,
            None => return STATUS_BAD_INPUT,
        };
        let name = match std::str::from_utf8(name_bytes) {
            Ok(s) => s,
            Err(_) => return STATUS_BAD_INPUT,
        };

        with_context(|ctx| {
            // Module::declare compiles + links synchronously (static imports are
            // resolved via HostResolver/HostLoader installed at engine init).
            let module = match Module::declare(ctx.clone(), name, source) {
                Ok(m) => m,
                Err(_) => {
                    if !out_handle.is_null() {
                        unsafe { *out_handle = NULL_HANDLE };
                    }
                    return STATUS_JS_ERROR;
                }
            };
            // eval() evaluates the module. Returns (Module<Evaluated>, Promise)
            // where the Promise resolves when TLA completes. We return the
            // module namespace as a handle; for TLA the host uses the same
            // eval_async drive loop.
            match module.eval() {
                Ok((evaluated, _promise)) => {
                    let ns = match evaluated.namespace() {
                        Ok(obj) => obj.into_value(),
                        Err(_) => {
                            if !out_handle.is_null() {
                                unsafe { *out_handle = NULL_HANDLE };
                            }
                            return STATUS_JS_ERROR;
                        }
                    };
                    let handle = mint_value(ctx, ns);
                    if !out_handle.is_null() {
                        unsafe { *out_handle = handle };
                    }
                    STATUS_OK
                }
                Err(_) => {
                    if !out_handle.is_null() {
                        unsafe { *out_handle = NULL_HANDLE };
                    }
                    STATUS_JS_ERROR
                }
            }
        })
        .unwrap_or(STATUS_NO_ENGINE)
    }));
    result.unwrap_or(STATUS_PANIC)
}

/// eval_module_async — compile + link a module, then return the EVALUATION
/// PROMISE (from `Module::eval`) as a handle. Unlike `eval_module` (which
/// discards the promise and returns the namespace synchronously), this is for
/// modules with **top-level await**: the host drives the promise via the same
/// `execute_pending_jobs` loop it uses for `eval_async`, settling async host
/// calls in between, until it resolves. The resolved value is `undefined`
/// (ES modules complete with undefined); the module's exports/side effects are
/// observed via globals or a subsequent import.
#[no_mangle]
pub extern "C" fn eval_module_async(
    code_ptr: *const u8,
    code_len: usize,
    name_ptr: *const u8,
    name_len: usize,
    out_handle: *mut i32,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let code_bytes = match read_input(code_ptr, code_len) {
            Some(b) => b,
            None => return STATUS_BAD_INPUT,
        };
        let source = match std::str::from_utf8(code_bytes) {
            Ok(s) => s,
            Err(_) => return STATUS_BAD_INPUT,
        };
        let name_bytes = match read_input(name_ptr, name_len) {
            Some(b) => b,
            None => return STATUS_BAD_INPUT,
        };
        let name = match std::str::from_utf8(name_bytes) {
            Ok(s) => s,
            Err(_) => return STATUS_BAD_INPUT,
        };
        with_context(|ctx| {
            let module = match Module::declare(ctx.clone(), name, source) {
                Ok(m) => m,
                Err(_) => {
                    if !out_handle.is_null() {
                        unsafe { *out_handle = NULL_HANDLE };
                    }
                    return STATUS_JS_ERROR;
                }
            };
            match module.eval() {
                Ok((_evaluated, promise)) => {
                    // Return the TLA promise; the host drives it to completion.
                    let handle = mint_value(ctx, promise.into_value());
                    if !out_handle.is_null() {
                        unsafe { *out_handle = handle };
                    }
                    STATUS_OK
                }
                Err(_) => {
                    if !out_handle.is_null() {
                        unsafe { *out_handle = NULL_HANDLE };
                    }
                    STATUS_JS_ERROR
                }
            }
        })
        .unwrap_or(STATUS_NO_ENGINE)
    }));
    result.unwrap_or(STATUS_PANIC)
}
