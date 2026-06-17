//! Async: deferred promises + the job pump + the async host-fn path
//!
//! Mirrors `quickjs-wasi`'s model: a host fn that needs to be async returns a
//! **deferred promise**; the host kicks off work; when it settles, the host
//! calls `resolve_deferred`/`reject_deferred` then `execute_pending_jobs()`
//! to drain the microtask queue so the JS `await` resumes.
//!
//! ## Deferred registry
//! `ctx.promise()` yields `(promise, resolve_fn, reject_fn)`. We stash the two
//! functions as `Persistent`s keyed by a monotonic `deferred_id` and hand the
//! id to the host. `resolve_deferred(id, value)` looks up the stashed resolve
//! fn and calls it; the entry is removed (single-settle).
//!
//! ## Async host fn
//! The async trampoline creates a deferred, calls the `host_call_async`
//! import (which returns immediately — the host records the pending dispatch),
//! and returns the promise to JS. Settlement happens later via the host
//! calling the resolve/reject exports.

use std::cell::RefCell;
use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};

use rquickjs::{Ctx, Function, Persistent, Value};

use crate::engine::{with_context, STATUS_BAD_INPUT, STATUS_NO_ENGINE, STATUS_OK, STATUS_PANIC};
use crate::handles::{mint_value, take_handle, NULL_HANDLE};

/// The stashed settle functions for one pending deferred.
struct Deferred {
    resolve: Persistent<Function<'static>>,
    reject: Persistent<Function<'static>>,
}

thread_local! {
    static DEFERREDS: RefCell<HashMap<u32, Deferred>> = RefCell::new(HashMap::new());
    static NEXT_ID: RefCell<u32> = const { RefCell::new(1) };
}

fn next_deferred_id() -> u32 {
    NEXT_ID.with(|n| {
        let mut id = n.borrow_mut();
        let cur = *id;
        *id = id.wrapping_add(1).max(1); // never reuse 0
        cur
    })
}

/// Create a deferred inside the live context: returns (deferred_id,
/// promise_handle). Stashes resolve/reject keyed by the id.
fn create_deferred<'js>(ctx: &Ctx<'js>) -> Option<(u32, i32)> {
    let (promise, resolve, reject) = ctx.promise().ok()?;
    let id = next_deferred_id();
    let entry = Deferred {
        resolve: Persistent::save(ctx, resolve),
        reject: Persistent::save(ctx, reject),
    };
    DEFERREDS.with(|d| d.borrow_mut().insert(id, entry));
    let promise_handle = mint_value(ctx, promise.into_value());
    Some((id, promise_handle))
}

/// Settle a deferred: look up its resolve/reject, call the chosen one with
/// `value`, remove the entry. `which` true=resolve, false=reject.
fn settle<'js>(id: u32, value: Value<'js>, which: bool, ctx: &Ctx<'js>) -> i32 {
    let entry = DEFERREDS.with(|d| d.borrow_mut().remove(&id));
    let entry = match entry {
        Some(e) => e,
        None => return STATUS_BAD_INPUT, // unknown / already-settled id (IDOR-safe)
    };
    let func = if which { entry.resolve } else { entry.reject };
    let func = match func.clone().restore(ctx) {
        Ok(f) => f,
        Err(_) => return STATUS_BAD_INPUT,
    };
    match func.call::<_, Value>((value,)) {
        Ok(_) => STATUS_OK,
        Err(_) => STATUS_BAD_INPUT,
    }
}

// ---------------------------------------------------------------------------
// Async eval: eval via JS_EVAL_FLAG_ASYNC (eval_promise) so top-level await +
// multi-statement bodies return their last-expression value. Returns the
// result PROMISE as a handle; the host drives execute_pending_jobs (settling
// async host calls in between) until it resolves, then reads promise_result.
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn eval_async(code_ptr: *const u8, code_len: usize, out_handle: *mut i32) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let bytes = match crate::mem::read_input(code_ptr, code_len) {
            Some(b) => b,
            None => return STATUS_BAD_INPUT,
        };
        let src = match std::str::from_utf8(bytes) {
            Ok(s) => s,
            Err(_) => return STATUS_BAD_INPUT,
        };
        with_context(|ctx| match ctx.eval_promise(src) {
            Ok(promise) => {
                // Returns the result PROMISE. It resolves to the `{ value, done }`
                // envelope (JS_EVAL_FLAG_ASYNC); the HOST unwraps `.value`.
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
                crate::engine::STATUS_JS_ERROR
            }
        })
        .unwrap_or(STATUS_NO_ENGINE)
    }));
    result.unwrap_or(STATUS_PANIC)
}

/// Promise introspection for the host's drive loop.
/// Writes the state (0 pending / 1 resolved / 2 rejected) to `*out_state`.
#[no_mangle]
pub extern "C" fn promise_state(handle: i32, out_state: *mut i32) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| {
            let value = match unsafe { crate::handles::borrow_for_promise(handle, ctx) } {
                Some(v) => v,
                None => return STATUS_BAD_INPUT,
            };
            let promise = match value.into_promise() {
                Some(p) => p,
                None => return STATUS_BAD_INPUT,
            };
            let state = match promise.state() {
                rquickjs::promise::PromiseState::Pending => 0,
                rquickjs::promise::PromiseState::Resolved => 1,
                rquickjs::promise::PromiseState::Rejected => 2,
            };
            if !out_state.is_null() {
                unsafe { *out_state = state };
            }
            STATUS_OK
        })
        .unwrap_or(STATUS_NO_ENGINE)
    }));
    result.unwrap_or(STATUS_PANIC)
}

/// Extract a settled promise's result as a new handle. For a RESOLVED promise
/// the fulfillment value is returned with STATUS_OK; for a REJECTED promise
/// the rejection reason is returned as a handle with STATUS_JS_ERROR (the host
/// distinguishes by status and/or promise_state). Pending → STATUS_BAD_INPUT.
#[no_mangle]
pub extern "C" fn promise_result(handle: i32, out_handle: *mut i32) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| {
            let value = match unsafe { crate::handles::borrow_for_promise(handle, ctx) } {
                Some(v) => v,
                None => return STATUS_BAD_INPUT,
            };
            let promise = match value.into_promise() {
                Some(p) => p,
                None => return STATUS_BAD_INPUT,
            };
            match promise.result::<Value>() {
                // Resolved: return the RAW fulfillment value. For eval_async
                // promises that is the `{ value, done }` envelope QuickJS's
                // JS_EVAL_FLAG_ASYNC produces — the HOST unwraps `.value` (the
                // public adapter owns that contract). The guest does NOT unwrap,
                // so promise_result is a single, honest "what did this promise
                // resolve to" with no special-casing.
                Some(Ok(v)) => {
                    write_handle(out_handle, mint_value(ctx, v));
                    STATUS_OK
                }
                // Rejected: `result()` threw the reason into the context; grab
                // it via ctx.catch() and return it as the reason handle.
                Some(Err(_)) => {
                    let reason = ctx.catch();
                    write_handle(out_handle, mint_value(ctx, reason));
                    crate::engine::STATUS_JS_ERROR
                }
                // Still pending.
                None => STATUS_BAD_INPUT,
            }
        })
        .unwrap_or(STATUS_NO_ENGINE)
    }));
    result.unwrap_or(STATUS_PANIC)
}

fn write_handle(out: *mut i32, handle: i32) {
    if !out.is_null() {
        unsafe { *out = handle };
    }
}

// ---------------------------------------------------------------------------
// Exports: new_promise / resolve_deferred / reject_deferred /
// execute_pending_jobs.
// ---------------------------------------------------------------------------

/// Create a deferred promise. Writes the deferred id to `*out_id` and returns
/// the promise handle (0 on failure).
#[no_mangle]
pub extern "C" fn new_promise(out_id: *mut u32) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| match create_deferred(ctx) {
            Some((id, handle)) => {
                if !out_id.is_null() {
                    unsafe { *out_id = id };
                }
                handle
            }
            None => NULL_HANDLE,
        })
        .unwrap_or(NULL_HANDLE)
    }));
    result.unwrap_or(NULL_HANDLE)
}

/// Resolve a deferred with the value referenced by `value_handle` (ownership
/// transferred to us). Returns STATUS_*.
#[no_mangle]
pub extern "C" fn resolve_deferred(id: u32, value_handle: i32) -> i32 {
    settle_export(id, value_handle, true)
}

/// Reject a deferred with the value referenced by `value_handle`.
#[no_mangle]
pub extern "C" fn reject_deferred(id: u32, value_handle: i32) -> i32 {
    settle_export(id, value_handle, false)
}

fn settle_export(id: u32, value_handle: i32, which: bool) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| {
            let value = match take_handle(value_handle, ctx) {
                Some(v) => v,
                None => return STATUS_BAD_INPUT,
            };
            settle(id, value, which, ctx)
        })
        .unwrap_or(STATUS_NO_ENGINE)
    }));
    result.unwrap_or(STATUS_PANIC)
}

/// Drain pending microtasks (promise reactions). Returns the count executed,
/// bounded to avoid an unbounded host stall on a pathological job storm.
#[no_mangle]
pub extern "C" fn execute_pending_jobs() -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| {
            let mut count = 0i32;
            // Bound the drain (spec: poll bounded). 100k matches the -spec cap.
            while count < 100_000 && ctx.execute_pending_job() {
                count += 1;
            }
            count
        })
        .unwrap_or(0)
    }));
    result.unwrap_or(0)
}
