//! Guest-side runtime/context registries, keyed by opaque u32 IDs.
//!
//! rquickjs `Runtime`/`Context` are not `Send`/`Sync`, but the wasm guest is
//! single-threaded, so thread-local tables are the natural home — they sidestep
//! the lifetime/Send friction a global registry would create. IDs are
//! monotonic; a closed slot is removed so a stale ID fails lookup
//! (invalid_runtime / invalid_context).

use rquickjs::{Context, Runtime};
use std::cell::RefCell;
use std::collections::HashMap;

struct RuntimeEntry {
    rt: Runtime,
}

thread_local! {
    static RUNTIMES: RefCell<HashMap<u32, RuntimeEntry>> = RefCell::new(HashMap::new());
    static CONTEXTS: RefCell<HashMap<u32, Context>> = RefCell::new(HashMap::new());
    static NEXT_ID: RefCell<u32> = const { RefCell::new(1) };
}

fn next_id() -> u32 {
    NEXT_ID.with(|n| {
        let mut n = n.borrow_mut();
        let id = *n;
        // Monotonic; wraps are not a concern within a trust-domain instance
        // lifetime (spec: one instance per trust domain). Skip 0 (reserved).
        *n = n.wrapping_add(1).max(1);
        id
    })
}

/// Create a runtime with the given memory limit (0 = engine default), return
/// its id, or None if creation failed.
pub(crate) fn runtime_new(memory_limit: usize) -> Option<u32> {
    let rt = Runtime::new().ok()?;
    if memory_limit != 0 {
        rt.set_memory_limit(memory_limit);
    }
    let id = next_id();
    RUNTIMES.with(|m| m.borrow_mut().insert(id, RuntimeEntry { rt }));
    Some(id)
}

/// Close (drop) a runtime. Returns false if the id was unknown.
pub(crate) fn runtime_close(id: u32) -> bool {
    RUNTIMES.with(|m| m.borrow_mut().remove(&id).is_some())
}

/// Create a context in the given runtime, return its id, or None if the
/// runtime id is unknown or context creation failed.
pub(crate) fn context_new(runtime_id: u32) -> Option<u32> {
    let ctx = RUNTIMES.with(|m| {
        let m = m.borrow();
        let entry = m.get(&runtime_id)?;
        Context::full(&entry.rt).ok()
    })?;
    let id = next_id();
    CONTEXTS.with(|m| m.borrow_mut().insert(id, ctx));
    Some(id)
}

/// Close (drop) a context. Returns false if the id was unknown.
pub(crate) fn context_close(id: u32) -> bool {
    CONTEXTS.with(|m| m.borrow_mut().remove(&id).is_some())
}

/// Run `f` with the context for `id`, or None if the id is unknown.
pub(crate) fn with_context<R>(id: u32, f: impl FnOnce(&Context) -> R) -> Option<R> {
    CONTEXTS.with(|m| {
        let m = m.borrow();
        let ctx = m.get(&id)?;
        Some(f(ctx))
    })
}
