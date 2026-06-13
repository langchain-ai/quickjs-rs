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

/// Stack-check probe. Evals deep self-recursion inside try/catch and
/// returns the depth at which QuickJS raised a catchable error. If the
/// module instead traps (wasm stack exhaustion), the host never gets a
/// return value and observes a Trap — that is the "permanently fatal"
/// outcome. A positive return = recursion is catchable (the limit fired);
/// negative sentinels = the engine surfaced a non-Range error.
///
/// `limit` is passed to `set_max_stack_size` first so we can measure
/// whether that call has any effect on wasi (expected: none by default,
/// because update_stack_limit zeroes stack_limit on __wasi__).
#[no_mangle]
pub extern "C" fn qrs_recurse_depth(limit: u32) -> i32 {
    let rt = match Runtime::new() {
        Ok(rt) => rt,
        Err(_) => return -1,
    };
    rt.set_max_stack_size(limit as usize);
    let ctx = match Context::full(&rt) {
        Ok(ctx) => ctx,
        Err(_) => return -2,
    };
    ctx.with(|ctx| {
        // Counts frames until QuickJS throws; the catch returns the depth.
        // If the C stack check never fires, this recurses until the wasm
        // stack is exhausted and the whole module traps before returning.
        let code = r#"
            (function () {
                let depth = 0;
                function rec() { depth++; rec(); }
                try { rec(); } catch (e) { return depth; }
                return -100; // returned without throwing: unexpected
            })()
        "#;
        match ctx.eval::<i32, _>(code) {
            Ok(v) => v,
            Err(_) => -3,
        }
    })
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
