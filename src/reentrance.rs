//! §6.7: reentrance-safe Ctx access.
//!
//! rquickjs's non-parallel runtime guard is a non-reentrant RefCell
//! (per-runtime, not per-context), so nested `Context::with` from
//! within a host-fn callback panics with "RefCell already borrowed".
//! We track which runtimes are currently entered on this thread:
//! reentrant calls on an active runtime skip `Context::with` and
//! build a temporary `Ctx` from the *requested* context pointer.
//!
//! The `'static` lifetime is a lie maintained by bracketing in
//! `with_active_ctx` — the slot is set on entry and cleared on exit
//! via an RAII guard, so no `Ctx` handed out from it ever outlives
//! its real `'js` scope. Removing or refactoring this machinery
//! will break `test_reentrant_eval_from_host_function` — the v0.2
//! tripwire that pinned it.
//!
//! This preserves reentrancy while keeping context identity correct
//! across sibling contexts sharing one runtime.
//!
//! Requires `panic = "unwind"` (enforced in Cargo.toml).
//! PyO3 wraps #[pymethods] calls in catch_unwind. Under panic=unwind, a
//! panicking host function unwinds through the Guard (clearing the thread-local)
//! before PyO3 catches it — the program recovers with a clean slot. Without
//! this, PyO3 could recover the program while the thread-local still holds a
//! dead Ctx<'static>, and the next reentrant call on that runtime would be a
//! use-after-free.

use pyo3::prelude::*;
use rquickjs::{Context, Ctx};
use std::cell::RefCell;
use std::collections::HashSet;

thread_local! {
    static ACTIVE_RUNTIMES: RefCell<HashSet<usize>> = RefCell::new(HashSet::new());
}

/// Run `f` with a live `Ctx<'js>` from `context`.
///
/// If this runtime is already active on the current thread (reentrant
/// call from a host fn), skip `Context::with` and synthesize a
/// temporary `Ctx` from `context`'s raw pointer under the already-held
/// runtime lock.
pub(crate) fn with_active_ctx<F, R>(context: &Context, f: F) -> PyResult<R>
where
    F: FnOnce(&Ctx<'_>) -> PyResult<R>,
{
    let rt_key = context.get_runtime_ptr() as usize;

    // Fast path: runtime lock is already held by an outer
    // with_active_ctx frame on this thread.
    let reentrant = ACTIVE_RUNTIMES.with(|cell| cell.borrow().contains(&rt_key));
    if reentrant {
        // Safety: presence in ACTIVE_RUNTIMES means we're running
        // inside an outer Context::with for this runtime. The raw
        // pointer is from `context` itself, and the borrowed Ctx
        // stays scoped to this call.
        let reentrant_ctx = unsafe { Ctx::from_raw(context.as_raw()) };
        return f(&reentrant_ctx);
    }

    // Slow path: enter Context::with and mark this runtime active
    // until we leave the closure (including unwind via Drop).
    context.with(|ctx| {
        ACTIVE_RUNTIMES.with(|cell| {
            cell.borrow_mut().insert(rt_key);
        });
        struct Guard(usize);
        impl Drop for Guard {
            fn drop(&mut self) {
                ACTIVE_RUNTIMES.with(|cell| {
                    cell.borrow_mut().remove(&self.0);
                });
            }
        }
        let _guard = Guard(rt_key);
        f(&ctx)
    })
}
