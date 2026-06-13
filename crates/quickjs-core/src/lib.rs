//! Build-feasibility spike for the WASM execution plane.
//!
//! Goal: prove that rquickjs links and a QuickJS context runs *inside* a
//! wasm32-wasip1 module built from source we control — nothing more. No
//! ABI codec, no host imports, no handle table. Two exports:
//!
//!   qrs_abi_version() -> u32     trivial: did the module load at all
//!   qrs_selftest()    -> i32     create Runtime+Context, eval "1 + 2"
//!
//! If qrs_selftest returns 3 from a freshly built module, the binding
//! decision (ADR 0001) is reproduced on the current toolchain and the
//! feasibility verdict is green.

use rquickjs::{Context, Runtime};

#[no_mangle]
pub extern "C" fn qrs_abi_version() -> u32 {
    1
}

/// Returns the integer result of evaluating `1 + 2` inside a fresh
/// QuickJS context, or a negative sentinel on failure so the harness can
/// distinguish "engine ran but eval failed" from "module trapped".
#[no_mangle]
pub extern "C" fn qrs_selftest() -> i32 {
    let rt = match Runtime::new() {
        Ok(rt) => rt,
        Err(_) => return -1,
    };
    let ctx = match Context::full(&rt) {
        Ok(ctx) => ctx,
        Err(_) => return -2,
    };
    ctx.with(|ctx| match ctx.eval::<i32, _>("1 + 2") {
        Ok(v) => v,
        Err(_) => -3,
    })
}
