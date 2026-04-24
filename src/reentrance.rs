//! Reentrance-safe Ctx access.
//!
//! rquickjs's non-parallel runtime guard is a non-reentrant RefCell
//! (per-runtime, not per-context), so nested `Context::with` from
//! within a host-fn callback panics with "RefCell already borrowed".
//! We stash the currently-active `Ctx<'static>` (lifetime laundered,
//! same pattern as rquickjs::Persistent) keyed by raw JSRuntime
//! pointer in a thread-local: any QjsHandle on any QjsContext
//! sharing that runtime picks up the stashed Ctx during reentrance
//! and skips the nested `with` entirely.
//!
//! The `'static` lifetime is a lie maintained by bracketing in
//! `with_active_ctx` — the slot is set on entry and cleared on exit
//! via an RAII guard, so no `Ctx` handed out from it ever outlives
//! its real `'js` scope. Removing or refactoring this machinery
//! will break `test_reentrant_eval_from_host_function`.
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
use std::collections::HashMap;

thread_local! {
    static ACTIVE_CTX_BY_RT: RefCell<HashMap<usize, Ctx<'static>>> =
        RefCell::new(HashMap::new());
}

/// Run `f` with a live `Ctx<'js>` from `context`. If there's already
/// an active Ctx for this runtime (reentrant call from a host fn),
/// use that directly instead of re-locking via `Context::with`.
pub(crate) fn with_active_ctx<F, R>(context: &Context, f: F) -> PyResult<R>
where
    F: FnOnce(&Ctx<'_>) -> PyResult<R>,
{
    let rt_key = context.get_runtime_ptr() as usize;

    // Fast path: reentrant call — use the stashed Ctx. We clone it
    // out of the map so the closure can borrow the clone; the
    // stashed entry stays in place for any deeper nesting.
    let stashed: Option<Ctx<'static>> =
        ACTIVE_CTX_BY_RT.with(|cell| cell.borrow().get(&rt_key).cloned());
    if let Some(stashed) = stashed {
        // Shrink 'static back down to the local borrow. Sound because
        // we're inside the outer with's closure body right now.
        let as_short: &Ctx<'_> =
            unsafe { core::mem::transmute::<&Ctx<'static>, &Ctx<'_>>(&stashed) };
        return f(as_short);
    }

    // Slow path: enter Context::with, publish the Ctx into the
    // thread-local, clear on exit (incl. panic unwind via RAII).
    context.with(|ctx| {
        let static_ctx: Ctx<'static> =
            unsafe { core::mem::transmute::<Ctx<'_>, Ctx<'static>>(ctx.clone()) };
        ACTIVE_CTX_BY_RT.with(|cell| {
            cell.borrow_mut().insert(rt_key, static_ctx);
        });
        struct Guard(usize);
        impl Drop for Guard {
            fn drop(&mut self) {
                ACTIVE_CTX_BY_RT.with(|cell| {
                    cell.borrow_mut().remove(&self.0);
                });
            }
        }
        let _guard = Guard(rt_key);
        f(&ctx)
    })
}
