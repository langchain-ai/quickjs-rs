//! Per-instance engine (single Runtime + Context) and the shared
//! status codes.
//!
//! A host function called from inside `eval` re-enters the guest: JS calls a
//! host fn → the `host_call` import → the host mints a result by calling
//! `new_number`/etc., which calls `with_context` AGAIN while the outer
//! `Context::with` is still live. Re-borrowing the engine `RefCell` (or
//! re-locking `Context::with`) panics ("RefCell already borrowed"). We
//! centralize a reentrancy check: a thread-local records the in-flight raw
//! `JSContext`; nested entries reconstruct a `Ctx` from it via
//! `Ctx::from_raw` instead of re-locking.

use std::cell::{Cell, RefCell};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr::NonNull;

use crate::modules::{HostLoader, HostResolver};
use rquickjs::{qjs, Context, Ctx, Runtime};

// Status codes (host reads these as the i32 return of fallible exports).
pub const STATUS_OK: i32 = 0;
pub const STATUS_JS_ERROR: i32 = 1;
pub const STATUS_BAD_INPUT: i32 = 2;
pub const STATUS_PANIC: i32 = 3;
pub const STATUS_NO_ENGINE: i32 = 4;

extern "C" {
    /// Host interrupt poll. Returns nonzero when the host wants
    /// execution to stop (its per-eval deadline elapsed). Called from the
    /// QuickJS interrupt handler on the guest's hot loop, so the host side
    /// MUST be O(1) and allocation-free. The deadline is host-owned — the
    /// guest cannot extend its own deadline.
    fn host_interrupt() -> i32;
}

struct Engine {
    _runtime: Runtime,
    context: Context,
}

thread_local! {
    static ENGINE: RefCell<Option<Engine>> = const { RefCell::new(None) };
    /// The raw JSContext of the in-flight `Context::with`, set while a
    /// top-level `with_context` body runs so nested (reentrant) calls can
    /// reuse it instead of re-locking. `None` when no eval is active.
    static CURRENT_RAW_CTX: Cell<Option<NonNull<qjs::JSContext>>> = const { Cell::new(None) };
    /// Requested limits, applied to the Runtime when it is created. The host
    /// may set these before the first eval (which is what creates the runtime).
    static MEMORY_LIMIT: Cell<Option<usize>> = const { Cell::new(None) };
    static STACK_LIMIT: Cell<Option<usize>> = const { Cell::new(None) };
}

/// Ensure the engine exists; returns STATUS_OK or STATUS_NO_ENGINE.
fn ensure_engine() -> i32 {
    ENGINE.with(|e| {
        let mut slot = e.borrow_mut();
        if slot.is_none() {
            let rt = match Runtime::new() {
                Ok(rt) => rt,
                Err(_) => return STATUS_NO_ENGINE,
            };
            // Install the synchronous module loader before creating the context
            // so any module eval (including via nested imports) can resolve.
            rt.set_loader(HostResolver, HostLoader);
            // Apply any host-requested resource limits (spec §5).
            if let Some(limit) = MEMORY_LIMIT.with(|c| c.get()) {
                rt.set_memory_limit(limit);
            }
            if let Some(limit) = STACK_LIMIT.with(|c| c.get()) {
                rt.set_max_stack_size(limit);
            }
            // Install the graceful interrupt handler: poll the host on the JS
            // hot loop; `true` unwinds JS with `InternalError: interrupted`
            // (the host maps it to TimeoutError) while the instance SURVIVES.
            rt.set_interrupt_handler(Some(Box::new(|| unsafe { host_interrupt() != 0 })));
            let ctx = match Context::full(&rt) {
                Ok(ctx) => ctx,
                Err(_) => return STATUS_NO_ENGINE,
            };
            *slot = Some(Engine {
                _runtime: rt,
                context: ctx,
            });
        }
        STATUS_OK
    })
}

/// Run `f` inside the engine's context. Returns `None` if the engine can't be
/// created. Auto-initializes the engine on first use.
///
/// Reentrancy-safe: if a context is already in-flight on this thread (we are
/// inside a host_call that re-entered an export), reuse the live `Ctx` via
/// `from_raw` rather than re-borrowing the engine.
pub fn with_context<R, F>(f: F) -> Option<R>
where
    F: FnOnce(&Ctx<'_>) -> R,
{
    // Reentrant path: a context is already live on this thread.
    if let Some(raw) = CURRENT_RAW_CTX.with(|c| c.get()) {
        // SAFETY: `raw` is the live JSContext of the enclosing Context::with,
        // valid for the duration of this nested call (single-threaded guest;
        // the outer borrow outlives us). We do not free it.
        let ctx = unsafe { Ctx::from_raw(raw) };
        return Some(f(&ctx));
    }

    // Top-level path: create/borrow the engine and run inside Context::with,
    // recording the raw ctx so nested calls take the reentrant path.
    if ensure_engine() != STATUS_OK {
        return None;
    }
    ENGINE.with(|e| {
        let slot = e.borrow();
        let engine = slot.as_ref()?;
        Some(engine.context.with(|ctx| {
            // Set the in-flight raw ctx; a drop guard clears it even if `f`
            // unwinds (panic=unwind), so we never leave a stale pointer.
            CURRENT_RAW_CTX.with(|c| c.set(Some(ctx.as_raw())));
            let _guard = CtxGuard;
            f(&ctx)
        }))
    })
}

/// Clears `CURRENT_RAW_CTX` on scope exit (including unwind).
struct CtxGuard;
impl Drop for CtxGuard {
    fn drop(&mut self) {
        CURRENT_RAW_CTX.with(|c| c.set(None));
    }
}

/// Run `f` with the live Runtime if the engine already exists (for applying a
/// limit immediately when set after init).
fn with_runtime<F: FnOnce(&Runtime)>(f: F) {
    ENGINE.with(|e| {
        if let Some(engine) = e.borrow().as_ref() {
            f(&engine._runtime);
        }
    });
}

/// Export: set the runtime memory limit (bytes). Stored for the runtime's
/// creation and applied immediately if it already exists. Exceeding it makes
/// QuickJS raise `InternalError: out of memory` → host `MemoryLimitError`.
#[no_mangle]
pub extern "C" fn set_memory_limit(limit: usize) {
    MEMORY_LIMIT.with(|c| c.set(Some(limit)));
    with_runtime(|rt| rt.set_memory_limit(limit));
}

/// Export: set the max JS stack size (bytes). Same apply-now-or-at-init policy.
#[no_mangle]
pub extern "C" fn set_max_stack_size(limit: usize) {
    STACK_LIMIT.with(|c| c.set(Some(limit)));
    with_runtime(|rt| rt.set_max_stack_size(limit));
}

/// Export: run the cyclic garbage collector. No-op if the engine isn't yet
/// created (nothing to collect).
#[no_mangle]
pub extern "C" fn run_gc() {
    with_runtime(|rt| rt.run_gc());
}

/// Export: compute QuickJS memory-usage stats. Writes the 26 `JSMemoryUsage`
/// fields as `i64` LE into the result buffer IN STRUCT ORDER (208 bytes); the
/// host reads them back in the same order keyed by the field names. The
/// struct-order convention is the single source of truth — no per-field tags.
#[no_mangle]
pub extern "C" fn compute_memory_usage() -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if ensure_engine() != STATUS_OK {
            return STATUS_NO_ENGINE;
        }
        ENGINE.with(|e| {
            let slot = e.borrow();
            let engine = match slot.as_ref() {
                Some(eng) => eng,
                None => return STATUS_NO_ENGINE,
            };
            let u = engine._runtime.memory_usage();
            // Fields in struct declaration order (mirrors JSMemoryUsage).
            let fields: [i64; 26] = [
                u.malloc_size,
                u.malloc_limit,
                u.memory_used_size,
                u.malloc_count,
                u.memory_used_count,
                u.atom_count,
                u.atom_size,
                u.str_count,
                u.str_size,
                u.obj_count,
                u.obj_size,
                u.prop_count,
                u.prop_size,
                u.shape_count,
                u.shape_size,
                u.js_func_count,
                u.js_func_size,
                u.js_func_code_size,
                u.js_func_pc2line_count,
                u.js_func_pc2line_size,
                u.c_func_count,
                u.array_count,
                u.fast_array_count,
                u.fast_array_elements,
                u.binary_object_count,
                u.binary_object_size,
            ];
            let mut bytes = Vec::with_capacity(fields.len() * 8);
            for f in fields {
                bytes.extend_from_slice(&f.to_le_bytes());
            }
            crate::mem::set_result(bytes);
            STATUS_OK
        })
    }));
    result.unwrap_or(STATUS_PANIC)
}
