//! `quickjs-core` — the WASM execution-plane guest.
//!
//! QuickJS (via rquickjs) runs inside this module's linear memory; the host
//! drives it through the `qrs_*` exports and the shared wire codec
//! (`quickjs-core-abi`). Phase 1 scope: ABI version, alloc/free, runtime and
//! context lifecycle, primitive eval. No host callbacks, modules, handles, or
//! snapshots yet.
//!
//! Boundary discipline: every export catches panics (`guest_panic` status
//! rather than a wasm abort) and validates all decoded input — the guest is
//! the thing on the trusted side of a copied-bytes boundary, but a panic that
//! crossed it would poison the instance.

mod marshal;
mod mem;
mod registry;

use quickjs_core_abi::{
    decode_envelope, encode_value, AbiResponse, OkShape, Reason, Status, Value, ABI_VERSION,
};
use std::panic::{catch_unwind, AssertUnwindSafe};

pub use mem::{qrs_alloc, qrs_free, qrs_response_free};

/// Report the wire ABI version the guest implements. Hosts check this and
/// fail fast on mismatch (no cross-version parsing).
#[no_mangle]
pub extern "C" fn qrs_abi_version() -> u32 {
    ABI_VERSION
}

/// Write a 16-byte `AbiResponse` descriptor to `out` (guest pointer the host
/// provided). Infallible given a valid 16-byte region.
fn write_descriptor(out: u32, resp: AbiResponse) {
    if out == 0 {
        return;
    }
    let bytes = quickjs_core_abi::encode_descriptor(&resp);
    // SAFETY: the host provides a 16-byte out region per the ABI contract.
    unsafe {
        std::ptr::copy_nonoverlapping(bytes.as_ptr(), out as usize as *mut u8, 16);
    }
}

/// A descriptor for a value payload: allocate guest bytes, set ok/tag.
fn ok_response(payload: &[u8], shape: OkShape) -> AbiResponse {
    let (ptr, len) = mem::write_guest(payload);
    AbiResponse { status: Status::Ok as u32, tag: shape as u32, ptr, len }
}

/// A descriptor for a thrown-JS-exception payload (an encoded Error value).
fn guest_error_response(payload: &[u8]) -> AbiResponse {
    let (ptr, len) = mem::write_guest(payload);
    AbiResponse { status: Status::GuestErrorResponse as u32, tag: 0, ptr, len }
}

/// An empty-payload descriptor for a bare status (no bytes).
fn bare(status: Status) -> AbiResponse {
    AbiResponse { status: status as u32, tag: 0, ptr: 0, len: 0 }
}

// --- runtime / context lifecycle -------------------------------------------

/// Create a runtime. Request payload is reserved for config (memory limit,
/// etc.); Phase 1 reads an optional memory_limit and ignores the rest. On
/// success the new runtime id is returned as a Number payload.
#[no_mangle]
pub extern "C" fn qrs_runtime_new(req_ptr: u32, req_len: u32, out: u32) {
    guard(out, || {
        // For Phase 1 the request is an optional envelope whose payload is a
        // Number = memory limit, or empty for the default.
        let memory_limit = match mem::read_guest(req_ptr, req_len) {
            Some(bytes) if !bytes.is_empty() => match decode_envelope(&bytes) {
                Ok(env) => match env.payload {
                    Value::Number(bits) => f64::from_bits(bits) as usize,
                    Value::Null | Value::Undefined => 0,
                    _ => 0,
                },
                Err(r) => return invalid(r),
            },
            Some(_) => 0,
            None => 0,
        };
        match registry::runtime_new(memory_limit) {
            Some(id) => id_response(id),
            None => bare(Status::ResourceExhausted),
        }
    });
}

#[no_mangle]
pub extern "C" fn qrs_runtime_close(runtime_id: u32, out: u32) {
    guard(out, || {
        if registry::runtime_close(runtime_id) {
            bare(Status::Ok)
        } else {
            bare(Status::InvalidRuntime)
        }
    });
}

#[no_mangle]
pub extern "C" fn qrs_context_new(runtime_id: u32, out: u32) {
    guard(out, || match registry::context_new(runtime_id) {
        Some(id) => id_response(id),
        None => bare(Status::InvalidRuntime),
    });
}

#[no_mangle]
pub extern "C" fn qrs_context_close(context_id: u32, out: u32) {
    guard(out, || {
        if registry::context_close(context_id) {
            bare(Status::Ok)
        } else {
            bare(Status::InvalidContext)
        }
    });
}

// --- eval -------------------------------------------------------------------

/// Evaluate JS source. Request is an Envelope whose payload is a String of
/// source. Result: ok(value) | guest_error_response(Error) | invalid_context
/// | resource_exhausted (memory limit) | invalid_request (bad bytes).
#[no_mangle]
pub extern "C" fn qrs_eval(context_id: u32, req_ptr: u32, req_len: u32, out: u32) {
    guard(out, || {
        let bytes = match mem::read_guest(req_ptr, req_len) {
            Some(b) => b,
            None => return invalid(Reason::Truncated),
        };
        let env = match decode_envelope(&bytes) {
            Ok(e) => e,
            Err(r) => return invalid(r),
        };
        let source = match env.payload {
            Value::String(s) => s,
            _ => return invalid(Reason::UnknownValueTag),
        };

        let result = registry::with_context(context_id, |ctx| {
            ctx.with(|c| {
                match c.eval::<rquickjs::Value, _>(source.as_bytes()) {
                    Ok(v) => match marshal::js_to_abi(&c, &v, 0) {
                        Ok(av) => EvalOutcome::Value(av),
                        Err(_) => EvalOutcome::Value(Value::Undefined),
                    },
                    Err(e) => EvalOutcome::Threw(marshal::js_error_to_abi(&c, e)),
                }
            })
        });

        match result {
            None => bare(Status::InvalidContext),
            Some(EvalOutcome::Value(v)) => match encode_value(&v) {
                Ok(bytes) => ok_response(&bytes, OkShape::Value),
                Err(_) => bare(Status::ResourceExhausted),
            },
            Some(EvalOutcome::Threw(err)) => match encode_value(&err) {
                Ok(bytes) => guest_error_response(&bytes),
                Err(_) => bare(Status::ResourceExhausted),
            },
        }
    });
}

enum EvalOutcome {
    Value(Value),
    Threw(Value),
}

// --- helpers ----------------------------------------------------------------

/// Wrap an export body: catch panics (→ guest_panic) and write the resulting
/// descriptor to `out`. The closure returns the AbiResponse to write.
fn guard(out: u32, body: impl FnOnce() -> AbiResponse) {
    let resp = match catch_unwind(AssertUnwindSafe(body)) {
        Ok(r) => r,
        Err(_) => bare(Status::GuestPanic),
    };
    write_descriptor(out, resp);
}

/// A decode failure: invalid_request with the reason carried in tag (S4.8).
fn invalid(reason: Reason) -> AbiResponse {
    quickjs_core_abi::invalid_request_descriptor(reason)
}

/// Encode a freshly-minted id as an ok(Number) response payload.
fn id_response(id: u32) -> AbiResponse {
    match encode_value(&Value::number(id as f64)) {
        Ok(bytes) => ok_response(&bytes, OkShape::Value),
        Err(_) => bare(Status::ResourceExhausted),
    }
}
