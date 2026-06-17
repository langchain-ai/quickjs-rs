//! Error channel
//!
//! After any export returns `STATUS_JS_ERROR`, the host takes the pending
//! exception as a handle via `last_exception` and reads its `name`/`message`/
//! `stack` off it with the handle ops + typed accessors. `new_error` mints a
//! real JS `Error` and `throw` raises a value into the context — together they
//! let a host fn reject/throw a *specific* JS error (vs the generic sanitized
//! `HostError` the trampoline produces on a plain failure).

use std::panic::{catch_unwind, AssertUnwindSafe};

use rquickjs::{Ctx, Value};

use crate::engine::{
    with_context, STATUS_BAD_INPUT, STATUS_JS_ERROR, STATUS_NO_ENGINE, STATUS_OK, STATUS_PANIC,
};
use crate::handles::{borrow_for_promise, mint_value, NULL_HANDLE};
use crate::mem::read_input;

/// Borrow a handle's value (non-consuming) inside the live context.
fn borrow<'js>(handle: i32, ctx: &Ctx<'js>) -> Option<Value<'js>> {
    unsafe { borrow_for_promise(handle, ctx) }
}

/// `guard_handle` shape (mirrors handles.rs): runs `f` in the context, writes
/// the produced handle to `out_handle`, returns the status. Kept local to the
/// error channel — it's a small, self-contained module.
fn guard_handle<F>(out_handle: *mut i32, f: F) -> i32
where
    F: FnOnce(&Ctx<'_>) -> (i32, i32),
{
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| f(ctx)).unwrap_or((STATUS_NO_ENGINE, NULL_HANDLE))
    }));
    let (status, handle) = result.unwrap_or((STATUS_PANIC, NULL_HANDLE));
    if !out_handle.is_null() {
        unsafe { *out_handle = handle };
    }
    status
}

fn guard<F>(f: F) -> i32
where
    F: FnOnce(&Ctx<'_>) -> i32,
{
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| f(ctx)).unwrap_or(STATUS_NO_ENGINE)
    }));
    result.unwrap_or(STATUS_PANIC)
}

/// Take the pending exception (the value `ctx.catch()` returns after a JS
/// error) as a handle. Returns STATUS_BAD_INPUT if nothing is pending — never
/// hands back a stale handle as an error.
#[no_mangle]
pub extern "C" fn last_exception(out_handle: *mut i32) -> i32 {
    guard_handle(out_handle, |ctx| {
        // Gate on has_exception() — the intent-revealing primitive — BEFORE
        // catch() consumes anything. `JS_GetException` with nothing pending
        // returns `null`, but a legitimately-thrown `null` is indistinguishable
        // from that; has_exception() disambiguates and avoids consuming state on
        // the no-pending path.
        if !ctx.has_exception() {
            return (STATUS_BAD_INPUT, NULL_HANDLE);
        }
        let caught = ctx.catch();
        (STATUS_OK, mint_value(ctx, caught))
    })
}

/// Mint a real JS `Error` with the given message (for a host fn that wants to
/// reject/throw a proper Error object).
#[no_mangle]
pub extern "C" fn new_error(msg_ptr: *const u8, msg_len: usize, out_handle: *mut i32) -> i32 {
    guard_handle(out_handle, |ctx| {
        let msg = match read_input(msg_ptr, msg_len).and_then(|b| std::str::from_utf8(b).ok()) {
            Some(s) => s,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        match rquickjs::Exception::from_message(ctx.clone(), msg) {
            Ok(exc) => (STATUS_OK, mint_value(ctx, exc.into_value())),
            Err(_) => (STATUS_JS_ERROR, NULL_HANDLE),
        }
    })
}

/// Throw a value into the context: sets the pending exception so the next
/// eval/call observes it. Returns STATUS_OK (the throw itself can't fail; the
/// *value* surfaces on the next operation).
#[no_mangle]
pub extern "C" fn throw(handle: i32) -> i32 {
    guard(|ctx| {
        let v = match borrow(handle, ctx) {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        let _ = ctx.throw(v);
        STATUS_OK
    })
}
