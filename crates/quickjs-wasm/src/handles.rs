//! Handle representation and the value-construction / handle-op exports
//!
//! A **handle** is a raw `i32` pointer to a `Box<Persistent<Value>>` on the
//! guest heap, mirroring `quickjs-wasi`'s raw `JSValue*` but with a
//! borrow-safe referent (the rquickjs `Persistent`, not a bare `JSValue`).
//!
//!   - mint:  `Box::into_raw(Box::new(Persistent::save(&ctx, value))) as i32`
//!   - use:   `(&*ptr).clone().restore(&ctx)` inside `ctx.with` → a `Value`
//!   - free:  `drop(Box::from_raw(ptr))` → `Persistent` drop → inner `Value`
//!            drop → `JS_FreeValue`
//!
//! Lifetime safety in V1 is the host-side `disposed` flag.
//!
//! Every export is `catch_unwind`-guarded and validates untrusted input
//! (`ptr`/`len`, the `argv` handle array) before use.

use std::panic::{catch_unwind, AssertUnwindSafe};

use rquickjs::{Array, ArrayBuffer, Ctx, Function, Object, Persistent, TypedArray, Value};

use crate::engine::{
    with_context, STATUS_BAD_INPUT, STATUS_JS_ERROR, STATUS_NO_ENGINE, STATUS_OK, STATUS_PANIC,
};
use crate::mem::{read_input, set_result};

/// Cap on host-supplied binary/bigint/string input lengths (DoS guard).
const MAX_INPUT_LEN: usize = 64 * 1024 * 1024; // 64 MiB

/// A boxed, borrow-detached JS value. The handle the host holds is
/// `Box::into_raw(Box::new(Handle)) as i32`.
pub type Handle = Persistent<Value<'static>>;

/// Null handle sentinel (mint failures, error paths). The host treats 0 as
/// "no handle" and never frees it.
pub const NULL_HANDLE: i32 = 0;

/// Mint a handle from a live `Value`, transferring ownership of its refcount
/// into the boxed `Persistent`. Returns 0 on failure.
fn mint<'js>(ctx: &Ctx<'js>, value: Value<'js>) -> i32 {
    let persistent = Persistent::save(ctx, value);
    Box::into_raw(Box::new(persistent)) as i32
}

/// Public mint, for other modules (e.g. host-fn trampoline) that need to
/// hand a value to the host as a handle.
pub fn mint_value<'js>(ctx: &Ctx<'js>, value: Value<'js>) -> i32 {
    mint(ctx, value)
}

/// Restore the value referenced by `handle` and **consume** the handle
/// (free its box). Use for handles whose ownership has been transferred to
/// us (e.g. a host_call result). Returns None for a null/invalid handle.
///
/// SAFETY: `handle` must be a live handle owned by the caller.
pub fn take_handle<'js>(handle: i32, ctx: &Ctx<'js>) -> Option<Value<'js>> {
    if handle == NULL_HANDLE {
        return None;
    }
    // Reclaim the box and restore its value before the box (and thus the
    // inner Value's JS ref) drops. clone().restore keeps a live ref in the
    // returned Value; the box drop releases the Persistent's own ref.
    let boxed = unsafe { Box::from_raw(handle as *mut Handle) };
    boxed.clone().restore(ctx).ok()
}

/// Restore the `Value` referenced by `handle` into the current context.
///
/// SAFETY: `handle` must be a live pointer previously returned by `mint` and
/// not yet freed. A stale or fabricated handle is undefined behavior
///
/// We `clone()` before `restore()` because `restore` consumes the
/// `Persistent`; the clone is one `JS_DupValue`, freed when the returned
/// `Value` drops at the end of the caller's `with` scope.
unsafe fn borrow_value<'js>(handle: i32, ctx: &Ctx<'js>) -> Option<Value<'js>> {
    if handle == NULL_HANDLE {
        return None;
    }
    let boxed = &*(handle as *const Handle);
    boxed.clone().restore(ctx).ok()
}

/// Public non-consuming borrow of a handle's value (for inspection — e.g.
/// promise state/result). Does not free the handle.
///
/// SAFETY: `handle` must be a live handle (see `borrow_value`).
pub unsafe fn borrow_for_promise<'js>(handle: i32, ctx: &Ctx<'js>) -> Option<Value<'js>> {
    borrow_value(handle, ctx)
}

/// Run `f` inside the engine context, returning a STATUS_*; wraps the
/// `catch_unwind` + no-engine + context-borrow boilerplate every export
/// shares. `f` returns a STATUS code.
fn guard<F>(f: F) -> i32
where
    F: FnOnce(&Ctx<'_>) -> i32,
{
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| f(ctx)).unwrap_or(STATUS_NO_ENGINE)
    }));
    result.unwrap_or(STATUS_PANIC)
}

/// Like `guard`, but for exports whose success value is a handle written to
/// `out_handle`. Returns STATUS_*; on non-OK, `*out_handle` is set to 0.
fn guard_handle<F>(out_handle: *mut i32, f: F) -> i32
where
    F: FnOnce(&Ctx<'_>) -> (i32, i32),
{
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| f(ctx)).unwrap_or((STATUS_NO_ENGINE, NULL_HANDLE))
    }));
    let (status, handle) = result.unwrap_or((STATUS_PANIC, NULL_HANDLE));
    if !out_handle.is_null() {
        // SAFETY: host passes a valid 4-byte out slot.
        unsafe { *out_handle = handle };
    }
    status
}

// ---------------------------------------------------------------------------
// Value construction (host -> guest heap). Each returns a handle directly
// (0 on failure) — these cannot JS-throw, so no status word needed.
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn new_undefined() -> i32 {
    guard_to_handle(|ctx| Some(Value::new_undefined(ctx.clone())))
}

#[no_mangle]
pub extern "C" fn new_null() -> i32 {
    guard_to_handle(|ctx| Some(Value::new_null(ctx.clone())))
}

#[no_mangle]
pub extern "C" fn new_bool(value: i32) -> i32 {
    guard_to_handle(move |ctx| Some(Value::new_bool(ctx.clone(), value != 0)))
}

/// Construct a number from its f64 bit pattern (avoids float ABI quirks; the
/// host sends `f64::to_bits`).
#[no_mangle]
pub extern "C" fn new_number(bits: u64) -> i32 {
    guard_to_handle(move |ctx| Some(Value::new_number(ctx.clone(), f64::from_bits(bits))))
}

/// Construct a string from untrusted utf8 bytes in linear memory.
#[no_mangle]
pub extern "C" fn new_string(ptr: *const u8, len: usize) -> i32 {
    guard_to_handle(move |ctx| {
        let bytes = read_input(ptr, len)?;
        let text = std::str::from_utf8(bytes).ok()?;
        rquickjs::String::from_str(ctx.clone(), text)
            .ok()
            .map(|s| s.into_value())
    })
}

/// Helper: run a closure that produces an `Option<Value>` inside the context
/// and mint a handle (0 on None / no-engine / panic).
fn guard_to_handle<F>(f: F) -> i32
where
    F: for<'js> FnOnce(&Ctx<'js>) -> Option<Value<'js>>,
{
    let result = catch_unwind(AssertUnwindSafe(|| {
        with_context(|ctx| match f(ctx) {
            Some(v) => mint(ctx, v),
            None => NULL_HANDLE,
        })
        .unwrap_or(NULL_HANDLE)
    }));
    result.unwrap_or(NULL_HANDLE)
}

// ---------------------------------------------------------------------------
// eval_code — the sole SYNC eval (quickjs-wasi's `evalCode`). Returns a
// HANDLE to the result; the host reads it via the typed accessors below
// (get_number/get_string/...). There is no value-returning eval variant (that
// was the removed `-spec` sugar).
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn eval_code(code_ptr: *const u8, code_len: usize, out_handle: *mut i32) -> i32 {
    guard_handle(out_handle, |ctx| {
        let bytes = match read_input(code_ptr, code_len) {
            Some(b) => b,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        let src = match std::str::from_utf8(bytes) {
            Ok(s) => s,
            Err(_) => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        match ctx.eval::<Value, _>(src) {
            Ok(v) => (STATUS_OK, mint(ctx, v)),
            Err(_) => (STATUS_JS_ERROR, NULL_HANDLE),
        }
    })
}

// ---------------------------------------------------------------------------
// global object.
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn global_object() -> i32 {
    guard_to_handle(|ctx| Some(ctx.globals().into_value()))
}

// ---------------------------------------------------------------------------
// Handle lifetime: dup + free.
// ---------------------------------------------------------------------------

/// Duplicate a handle — a second independent owner of the same JS value. The
/// host must free both.
#[no_mangle]
pub extern "C" fn dup_handle(handle: i32) -> i32 {
    guard_to_handle(move |ctx| unsafe { borrow_value(handle, ctx) })
}

/// Free a handle: drop the boxed `Persistent`, releasing its JS refcount.
/// Idempotent only by host discipline (the `disposed` flag) — double-free of
/// the same raw pointer is UB in V1 (§2.2).
#[no_mangle]
pub extern "C" fn free_value(handle: i32) {
    if handle == NULL_HANDLE {
        return;
    }
    let _ = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: reclaim the box the matching mint leaked. Host guarantees
        // single-free via its disposed flag.
        let boxed = unsafe { Box::from_raw(handle as *mut Handle) };
        drop(boxed);
    }));
}

// ---------------------------------------------------------------------------
// Introspection: type_of, is_promise.
// ---------------------------------------------------------------------------

/// Write the value's type NAME (utf8) into the result buffer — the host reads
/// it via `qjs_last_ptr`/`qjs_last_len` + `qjs_result_free`, like `get_string`.
///
/// Returning the canonical string (not a numeric tag) keeps the type vocabulary
/// in ONE place — the guest — instead of a hand-maintained integer enum
/// duplicated across the host adapter and every harness (the drift trap).
/// Names match `typeof`-ish JS conventions plus explicit bigint/symbol/array.
#[no_mangle]
pub extern "C" fn type_of(handle: i32) -> i32 {
    guard(|ctx| {
        let value = match unsafe { borrow_value(handle, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        crate::mem::set_result(type_name(&value).as_bytes().to_vec());
        STATUS_OK
    })
}

#[no_mangle]
pub extern "C" fn is_promise(handle: i32, out_bool: *mut i32) -> i32 {
    guard(|ctx| {
        let value = match unsafe { borrow_value(handle, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        if !out_bool.is_null() {
            unsafe { *out_bool = value.is_promise() as i32 };
        }
        STATUS_OK
    })
}

/// Canonical type name for a value. The single source of truth for the type
/// vocabulary (host + harnesses read this string; no numeric enum to drift).
fn type_name(v: &Value) -> &'static str {
    if v.is_undefined() {
        "undefined"
    } else if v.is_null() {
        "null"
    } else if v.is_bool() {
        "boolean"
    } else if v.is_number() {
        "number"
    } else if v.is_string() {
        "string"
    } else if v.is_big_int() {
        "bigint"
    } else if v.is_symbol() {
        "symbol"
    } else if v.is_function() {
        "function"
    } else if v.is_array() {
        "array"
    } else if v.is_object() {
        "object"
    } else {
        "unknown"
    }
}

// ---------------------------------------------------------------------------
// Object operations: get_prop / get_index / set_prop / has_prop.
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn get_prop(
    obj: i32,
    key_ptr: *const u8,
    key_len: usize,
    out_handle: *mut i32,
) -> i32 {
    guard_handle(out_handle, |ctx| {
        let key = match read_str(key_ptr, key_len) {
            Some(k) => k,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        let object = match as_object(obj, ctx) {
            Some(o) => o,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        match object.get::<_, Value>(key) {
            Ok(v) => (STATUS_OK, mint(ctx, v)),
            Err(_) => (STATUS_JS_ERROR, NULL_HANDLE),
        }
    })
}

#[no_mangle]
pub extern "C" fn get_index(obj: i32, index: u32, out_handle: *mut i32) -> i32 {
    guard_handle(out_handle, |ctx| {
        let object = match as_object(obj, ctx) {
            Some(o) => o,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        match object.get::<_, Value>(index) {
            Ok(v) => (STATUS_OK, mint(ctx, v)),
            Err(_) => (STATUS_JS_ERROR, NULL_HANDLE),
        }
    })
}

#[no_mangle]
pub extern "C" fn set_prop(obj: i32, key_ptr: *const u8, key_len: usize, value: i32) -> i32 {
    guard(|ctx| {
        let key = match read_str(key_ptr, key_len) {
            Some(k) => k,
            None => return STATUS_BAD_INPUT,
        };
        let object = match as_object(obj, ctx) {
            Some(o) => o,
            None => return STATUS_BAD_INPUT,
        };
        let val = match unsafe { borrow_value(value, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        match object.set(key, val) {
            Ok(()) => STATUS_OK,
            Err(_) => STATUS_JS_ERROR,
        }
    })
}

#[no_mangle]
pub extern "C" fn has_prop(
    obj: i32,
    key_ptr: *const u8,
    key_len: usize,
    out_bool: *mut i32,
) -> i32 {
    guard(|ctx| {
        let key = match read_str(key_ptr, key_len) {
            Some(k) => k,
            None => return STATUS_BAD_INPUT,
        };
        let object = match as_object(obj, ctx) {
            Some(o) => o,
            None => return STATUS_BAD_INPUT,
        };
        match object.contains_key(key) {
            Ok(b) => {
                if !out_bool.is_null() {
                    unsafe { *out_bool = b as i32 };
                }
                STATUS_OK
            }
            Err(_) => STATUS_JS_ERROR,
        }
    })
}

// ---------------------------------------------------------------------------
// call_function / call_constructor.
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn call_function(
    func: i32,
    this: i32,
    argv_ptr: *const i32,
    argc: u32,
    out_handle: *mut i32,
) -> i32 {
    guard_handle(out_handle, |ctx| {
        let f = match as_function(func, ctx) {
            Some(f) => f,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        let args = match build_args(ctx, this, argv_ptr, argc) {
            Some(a) => a,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        match f.call_arg::<Value>(args) {
            Ok(v) => (STATUS_OK, mint(ctx, v)),
            Err(_) => (STATUS_JS_ERROR, NULL_HANDLE),
        }
    })
}

#[no_mangle]
pub extern "C" fn call_constructor(
    func: i32,
    argv_ptr: *const i32,
    argc: u32,
    out_handle: *mut i32,
) -> i32 {
    guard_handle(out_handle, |ctx| {
        // Construction requires a `Constructor`, a distinct rquickjs type from
        // `Function` (only constructors can be `new`-ed).
        let ctor = match unsafe { borrow_value(func, ctx) }.and_then(|v| v.into_constructor()) {
            Some(c) => c,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        let mut args = rquickjs::function::Args::new(ctx.clone(), argc as usize);
        if read_argv(argv_ptr, argc, ctx, &mut args).is_none() {
            return (STATUS_BAD_INPUT, NULL_HANDLE);
        }
        match args.construct::<Value>(&ctor) {
            Ok(v) => (STATUS_OK, mint(ctx, v)),
            Err(_) => (STATUS_JS_ERROR, NULL_HANDLE),
        }
    })
}

// ---------------------------------------------------------------------------
// Internal helpers.
// ---------------------------------------------------------------------------

/// Read + validate an untrusted utf8 key from linear memory.
fn read_str<'a>(ptr: *const u8, len: usize) -> Option<&'a str> {
    let bytes = read_input(ptr, len)?;
    std::str::from_utf8(bytes).ok()
}

fn as_object<'js>(handle: i32, ctx: &Ctx<'js>) -> Option<Object<'js>> {
    let value = unsafe { borrow_value(handle, ctx) }?;
    value.into_object()
}

fn as_function<'js>(handle: i32, ctx: &Ctx<'js>) -> Option<Function<'js>> {
    let value = unsafe { borrow_value(handle, ctx) }?;
    value.into_function()
}

/// Build a full `Args` (this + argv handles) for `call_arg`.
fn build_args<'js>(
    ctx: &Ctx<'js>,
    this: i32,
    argv_ptr: *const i32,
    argc: u32,
) -> Option<rquickjs::function::Args<'js>> {
    let this_val = unsafe { borrow_value(this, ctx) }?;
    let mut args = rquickjs::function::Args::new(ctx.clone(), argc as usize);
    args.this(this_val).ok()?;
    read_argv(argv_ptr, argc, ctx, &mut args)?;
    Some(args)
}

/// Read `argc` handle ids from `argv_ptr`, restore each, push as an arg.
/// Bound-checks the array region implicitly: a bad ptr/len traps in the
/// sandbox (contained), and each handle is validated by borrow_value.
fn read_argv<'js>(
    argv_ptr: *const i32,
    argc: u32,
    ctx: &Ctx<'js>,
    args: &mut rquickjs::function::Args<'js>,
) -> Option<()> {
    if argc == 0 {
        return Some(());
    }
    if argv_ptr.is_null() {
        return None;
    }
    // SAFETY: host passes a contiguous array of `argc` i32 handles. An
    // out-of-range read traps in the sandbox rather than corrupting the host.
    let ids = unsafe { std::slice::from_raw_parts(argv_ptr, argc as usize) };
    for &id in ids {
        let v = unsafe { borrow_value(id, ctx) }?;
        args.push_arg(v).ok()?;
    }
    Some(())
}

// ===========================================================================
// Typed value extraction (guest → host) — `quickjs-wasi`-style typed accessors.
//
// The host already knows (via `type_of`) what it expects and asks for exactly
// that, getting STATUS_BAD_INPUT if the value isn't that type. Scalars write to
// an out-pointer; strings/bigints/binary write to the result buffer (the host
// reads via qjs_last_ptr/len then qjs_result_free). These are the symmetric
// inverse of the `new_*` constructors above. A generic `dump` wire format
// previously served the bootstrap harnesses; it was removed once these +
// the Python adapter proved out.
// ===========================================================================

/// Write the f64 bit pattern of a number handle to `*out_bits`.
#[no_mangle]
pub extern "C" fn get_number(handle: i32, out_bits: *mut u64) -> i32 {
    guard(|ctx| {
        let v = match unsafe { borrow_value(handle, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        // as_number coerces int|float to f64; reject non-numbers.
        match v.as_number() {
            Some(f) => {
                if !out_bits.is_null() {
                    unsafe { *out_bits = f.to_bits() };
                }
                STATUS_OK
            }
            None => STATUS_BAD_INPUT,
        }
    })
}

/// Write a bool handle's value (0/1) to `*out`.
#[no_mangle]
pub extern "C" fn get_bool(handle: i32, out: *mut i32) -> i32 {
    guard(|ctx| {
        let v = match unsafe { borrow_value(handle, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        match v.as_bool() {
            Some(b) => {
                if !out.is_null() {
                    unsafe { *out = b as i32 };
                }
                STATUS_OK
            }
            None => STATUS_BAD_INPUT,
        }
    })
}

/// Marshal a string handle's utf8 into the result buffer.
#[no_mangle]
pub extern "C" fn get_string(handle: i32) -> i32 {
    guard(|ctx| {
        let v = match unsafe { borrow_value(handle, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        let s = match v.as_string() {
            Some(s) => s,
            None => return STATUS_BAD_INPUT,
        };
        match s.to_string() {
            Ok(text) => {
                set_result(text.into_bytes());
                STATUS_OK
            }
            Err(_) => STATUS_JS_ERROR,
        }
    })
}

/// Marshal a BigInt handle's DECIMAL STRING into the result buffer (via the JS
/// global `String(v)` — arbitrary precision; the inverse of `new_bigint`).
#[no_mangle]
pub extern "C" fn get_bigint(handle: i32) -> i32 {
    guard(|ctx| {
        let v = match unsafe { borrow_value(handle, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        if !v.is_big_int() {
            return STATUS_BAD_INPUT;
        }
        match string_of(ctx, v) {
            Some(text) => {
                set_result(text.into_bytes());
                STATUS_OK
            }
            None => STATUS_JS_ERROR,
        }
    })
}

/// Drain any pending exception. The rquickjs `from_object` type converters
/// (ArrayBuffer/TypedArray) call QuickJS ops that THROW into the context when
/// the value isn't that type (e.g. "ArrayBuffer object expected"), then return
/// `None`/`Err`. A type-PROBE must not poison the context with that exception —
/// the host treats a failed probe as "not this type", not a JS error. So we
/// clear it before returning a clean BAD_INPUT.
fn clear_pending(ctx: &Ctx<'_>) {
    if ctx.has_exception() {
        let _ = ctx.catch();
    }
}

/// Marshal an ArrayBuffer handle's raw bytes into the result buffer.
#[no_mangle]
pub extern "C" fn get_arraybuffer(handle: i32) -> i32 {
    guard(|ctx| {
        let v = match unsafe { borrow_value(handle, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        let ab = match v.into_object().and_then(ArrayBuffer::from_object) {
            Some(ab) => ab,
            None => {
                clear_pending(ctx); // probe miss — don't poison the context
                return STATUS_BAD_INPUT;
            }
        };
        match ab.as_bytes() {
            Some(bytes) => {
                set_result(bytes.to_vec());
                STATUS_OK
            }
            // Detached buffer → no bytes.
            None => {
                clear_pending(ctx);
                STATUS_BAD_INPUT
            }
        }
    })
}

/// Inspect a TypedArray (Uint8Array): write byte_offset/byte_length/bpe to the
/// out pointers and stash the bytes into the result buffer for a one-call read.
/// Returns BAD_INPUT if the handle is not a u8 typed array.
#[no_mangle]
pub extern "C" fn get_typed_array_buffer(
    handle: i32,
    out_byte_offset: *mut u32,
    out_byte_len: *mut u32,
    out_bpe: *mut u32,
) -> i32 {
    guard(|ctx| {
        let v = match unsafe { borrow_value(handle, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        // Uint8Array is the canonical binary view; cover it directly. Other
        // element types are reachable host-side via the buffer + a JS view.
        let ta = match v
            .into_object()
            .and_then(|o| TypedArray::<u8>::from_object(o).ok())
        {
            Some(ta) => ta,
            None => {
                clear_pending(ctx); // probe miss — don't poison the context
                return STATUS_BAD_INPUT;
            }
        };
        let byte_len = ta.len() as u32; // u8 → 1 byte/elem
        if !out_byte_offset.is_null() {
            unsafe { *out_byte_offset = 0 };
        }
        if !out_byte_len.is_null() {
            unsafe { *out_byte_len = byte_len };
        }
        if !out_bpe.is_null() {
            unsafe { *out_bpe = 1 };
        }
        match ta.as_bytes() {
            Some(bytes) => {
                set_result(bytes.to_vec());
                STATUS_OK
            }
            None => {
                clear_pending(ctx);
                STATUS_BAD_INPUT
            }
        }
    })
}

// ---------------------------------------------------------------------------
// More value construction: objects/arrays/binary/bigint + set_index.
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn new_object() -> i32 {
    guard_to_handle(|ctx| Object::new(ctx.clone()).ok().map(|o| o.into_value()))
}

#[no_mangle]
pub extern "C" fn new_array() -> i32 {
    guard_to_handle(|ctx| Array::new(ctx.clone()).ok().map(|a| a.into_value()))
}

/// Construct an ArrayBuffer from host bytes (always copied in).
#[no_mangle]
pub extern "C" fn new_arraybuffer(ptr: *const u8, len: usize, out_handle: *mut i32) -> i32 {
    guard_handle(out_handle, |ctx| {
        let bytes = match read_capped(ptr, len) {
            Some(b) => b,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        match ArrayBuffer::new_copy(ctx.clone(), bytes) {
            Ok(ab) => (STATUS_OK, mint(ctx, ab.into_value())),
            Err(_) => (STATUS_JS_ERROR, NULL_HANDLE),
        }
    })
}

/// Construct a Uint8Array from host bytes (the common binary case).
#[no_mangle]
pub extern "C" fn new_uint8array(ptr: *const u8, len: usize, out_handle: *mut i32) -> i32 {
    guard_handle(out_handle, |ctx| {
        let bytes = match read_capped(ptr, len) {
            Some(b) => b,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        match TypedArray::<u8>::new_copy(ctx.clone(), bytes) {
            Ok(ta) => (STATUS_OK, mint(ctx, ta.into_value())),
            Err(_) => (STATUS_JS_ERROR, NULL_HANDLE),
        }
    })
}

/// Construct a BigInt from a DECIMAL STRING (arbitrary precision) via the JS
/// global `BigInt("<decimal>")` — never `from_i64` (the truncation trap).
#[no_mangle]
pub extern "C" fn new_bigint(ptr: *const u8, len: usize, out_handle: *mut i32) -> i32 {
    guard_handle(out_handle, |ctx| {
        let bytes = match read_capped(ptr, len) {
            Some(b) => b,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        let decimal = match std::str::from_utf8(&bytes) {
            Ok(s) => s,
            Err(_) => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        let bigint_fn: Function = match ctx.globals().get("BigInt") {
            Ok(f) => f,
            Err(_) => return (STATUS_NO_ENGINE, NULL_HANDLE),
        };
        match bigint_fn.call::<_, Value>((decimal,)) {
            Ok(v) => (STATUS_OK, mint(ctx, v)),
            Err(_) => (STATUS_JS_ERROR, NULL_HANDLE),
        }
    })
}

/// Set an array/object element by integer index.
#[no_mangle]
pub extern "C" fn set_index(obj: i32, index: u32, value: i32) -> i32 {
    guard(|ctx| {
        let object = match unsafe { borrow_value(obj, ctx) }.and_then(|v| v.into_object()) {
            Some(o) => o,
            None => return STATUS_BAD_INPUT,
        };
        let val = match unsafe { borrow_value(value, ctx) } {
            Some(v) => v,
            None => return STATUS_BAD_INPUT,
        };
        match object.set(index, val) {
            Ok(()) => STATUS_OK,
            Err(_) => STATUS_JS_ERROR,
        }
    })
}

/// Own enumerable string keys of an object, as a JS Array the host iterates to
/// marshal an arbitrary object *out*.
#[no_mangle]
pub extern "C" fn get_own_property_names(obj: i32, out_handle: *mut i32) -> i32 {
    guard_handle(out_handle, |ctx| {
        let object = match unsafe { borrow_value(obj, ctx) }.and_then(|v| v.into_object()) {
            Some(o) => o,
            None => return (STATUS_BAD_INPUT, NULL_HANDLE),
        };
        let arr = match Array::new(ctx.clone()) {
            Ok(a) => a,
            Err(_) => return (STATUS_JS_ERROR, NULL_HANDLE),
        };
        let mut i = 0usize;
        for key in object.keys::<String>() {
            match key {
                Ok(k) => {
                    if arr.set(i, k).is_err() {
                        return (STATUS_JS_ERROR, NULL_HANDLE);
                    }
                    i += 1;
                }
                Err(_) => return (STATUS_JS_ERROR, NULL_HANDLE),
            }
        }
        (STATUS_OK, mint(ctx, arr.into_value()))
    })
}

// ---------------------------------------------------------------------------
// Extraction helpers.
// ---------------------------------------------------------------------------

/// Read + length-cap an untrusted host byte slice (copied to an owned Vec so the
/// constructors can hold it past the borrow). None on null/zero/oversize.
fn read_capped(ptr: *const u8, len: usize) -> Option<Vec<u8>> {
    if len > MAX_INPUT_LEN {
        return None;
    }
    read_input(ptr, len).map(|b| b.to_vec())
}

/// `String(v)` via the JS global — used for BigInt's arbitrary-precision
/// decimal stringification.
fn string_of<'js>(ctx: &Ctx<'js>, v: Value<'js>) -> Option<String> {
    let string_fn: Function = ctx.globals().get("String").ok()?;
    let s: rquickjs::String = string_fn.call((v,)).ok()?;
    s.to_string().ok()
}
