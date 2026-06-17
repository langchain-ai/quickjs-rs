//! Host functions: `new_function` + the `env.host_call` trampoline
//!
//! `new_function(name)` mints a JS function whose closure captures the
//! registered **name**. When JS calls it, the closure:
//!   1. mints transient handles for `this` and each argument,
//!   2. writes the name + the argv handle array into guest memory,
//!   3. calls the imported `host_call(name_ptr, name_len, this, argc, argv)`,
//!   4. receives a **result handle** the host minted (or a sentinel for a
//!      host-side error),
//!   5. restores it to a `Value`, frees the transient handles, returns it.
//!
//! Dispatch is **by name, host-side**. The guest is name-agnostic;
//! it just forwards. Name + arg handles are the only things crossing.
//!
//! ## Security
//! - The host treats the name as an untrusted lookup key (it is guest-bound
//!   here, but the host still bound-checks the read).
//! - Unknown name / host error → the host returns the error sentinel; the
//!   trampoline throws a sanitized JS error (no host detail).
//! - Transient handles are always freed, success or throw.

use std::panic::{catch_unwind, AssertUnwindSafe};

use rquickjs::function::{Rest, This};
use rquickjs::{Ctx, Function, Value};

use crate::engine::with_context;
use crate::handles::{mint_value, take_handle, NULL_HANDLE};
use crate::mem::{read_input, with_scratch};

extern "C" {
    /// Host callback dispatch. Returns a result handle the host minted, or
    /// `HOST_CALL_ERROR` to signal a host-side failure (unknown name, callback
    /// raised). Reading the name/args is the host's job.
    fn host_call(
        name_ptr: *const u8,
        name_len: usize,
        this_handle: i32,
        argc: u32,
        argv_ptr: *const i32,
    ) -> i32;
}

/// Sentinel the host returns from `host_call` to signal an error rather than
/// a value. Distinct from NULL_HANDLE (0) so a host that legitimately wants
/// to return `undefined` can mint an undefined handle instead.
const HOST_CALL_ERROR: i32 = -1;

/// Export: mint a JS function backed by the host, identified by `name`.
/// Returns a handle (0 on failure).
///
/// There is no sync/async distinction at registration: the single trampoline
/// returns whatever handle the host returns from `host_call`. For async, the
/// host returns a PROMISE handle (obtained via `new_promise`) and settles it
/// later (resolve_deferred + execute_pending_jobs) — matching quickjs-wasi.
#[no_mangle]
pub extern "C" fn new_function(name_ptr: *const u8, name_len: usize) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let name = match read_input(name_ptr, name_len).and_then(|b| std::str::from_utf8(b).ok()) {
            Some(s) => s.to_owned(),
            None => return NULL_HANDLE,
        };
        with_context(|ctx| match make_host_function(ctx, name) {
            Some(f) => mint_value(ctx, f.into_value()),
            None => NULL_HANDLE,
        })
        .unwrap_or(NULL_HANDLE)
    }));
    result.unwrap_or(NULL_HANDLE)
}

/// Build the trampoline closure for a registered `name` and wrap it as a JS
/// `Function`. Called by the `new_function` export.
pub fn make_host_function<'js>(ctx: &Ctx<'js>, name: String) -> Option<Function<'js>> {
    let ctx_owned = ctx.clone();
    Function::new(
        ctx.clone(),
        move |this: This<Value<'js>>, args: Rest<Value<'js>>| -> rquickjs::Result<Value<'js>> {
            trampoline(&ctx_owned, &name, this.0, args.0)
        },
    )
    .ok()
}

/// The per-call trampoline body: marshal this+args to handles, call the
/// host, restore the result.
fn trampoline<'js>(
    ctx: &Ctx<'js>,
    name: &str,
    this: Value<'js>,
    args: Vec<Value<'js>>,
) -> rquickjs::Result<Value<'js>> {
    // 1. Mint transient handles. These are owned by us for the duration of
    //    the host call and freed below regardless of outcome.
    let this_handle = mint_value(ctx, this);
    let arg_handles: Vec<i32> = args.into_iter().map(|v| mint_value(ctx, v)).collect();

    // 2. Write name + argv array into a scratch region and call the host.
    let result_handle = with_scratch(
        name.as_bytes(),
        &arg_handles,
        |name_ptr, name_len, argv_ptr, argc| {
            // SAFETY: import provided by the host at instantiation. All pointers
            // are into our own linear memory and live for the call's duration.
            unsafe { host_call(name_ptr, name_len, this_handle, argc, argv_ptr) }
        },
    );

    // 3. Free the transient argument + this handles (the host has marshalled
    //    out whatever it needs; handles are call-scoped).
    free_transient(this_handle);
    for h in &arg_handles {
        free_transient(*h);
    }

    // 4. Interpret the result.
    if result_handle == HOST_CALL_ERROR {
        // The host signals failure. If it already set a PENDING EXCEPTION
        // (via the `throw`/`new_error` exports — the host wants a SPECIFIC
        // name/message, e.g. context.py's sanitized "HostError"/"Host function
        // failed" so its side-channel can unwrap to the original Python
        // exception), propagate that. Otherwise (unknown name, host couldn't
        // build an error) fall back to a generic, sanitized HostError.
        if ctx.has_exception() {
            return Err(rquickjs::Error::Exception);
        }
        return Err(throw_host_error(ctx, name));
    }
    // The host minted result_handle and transferred ownership to us; take it
    // (restore + free the box) into a Value.
    match take_handle(result_handle, ctx) {
        Some(v) => Ok(v),
        None => Err(throw_host_error(ctx, name)),
    }
}

/// Free a transient handle minted for a host call (box drop → JS_FreeValue).
fn free_transient(handle: i32) {
    if handle != NULL_HANDLE {
        crate::handles::free_value(handle);
    }
}

/// Throw a sanitized host error into JS — a real `Error` object with
/// `.name === "HostError"` and a FIXED message, so JS can `instanceof Error` /
/// branch on the name while carrying NO host internals.
///
/// The message is a constant — deliberately NOT the function name or any
/// host-authored string. The host never authors a JS-visible string on the
/// failure path: it returns the `HOST_CALL_ERROR` sentinel and the *guest*
/// synthesizes this message wholesale, so there is no host→guest leak surface.
/// The original host exception (Python `RuntimeError`, etc.) stays host-side and
/// surfaces only at the host's own eval boundary, never inside JS. The fixed
/// string matches the host adapter's sanitized-message constant so its
/// side-channel can recognize a host raise and unwrap to the original there.
fn throw_host_error<'js>(ctx: &Ctx<'js>, _fn_name: &str) -> rquickjs::Error {
    match rquickjs::Exception::from_message(ctx.clone(), "Host function failed") {
        Ok(exc) => {
            let _ = exc.as_object().set("name", "HostError");
            exc.throw()
        }
        Err(_) => rquickjs::Error::Exception,
    }
}
