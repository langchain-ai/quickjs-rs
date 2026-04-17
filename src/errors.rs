//! §10 exception hierarchy + helpers that convert rquickjs errors
//! into PyErr. PyO3 needs native classes to raise; the Python side
//! re-exports these names from `quickjs_rs.errors` (see
//! `quickjs_rs/__init__.py`). Rust error conversion lives here too
//! so every call site routes through the same mapping.

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use rquickjs::{
    atom::PredefinedAtom,
    convert::{Coerced, FromJs},
    CaughtError, Ctx, Error,
};

create_exception!(_engine, QuickJSError, PyException);
create_exception!(_engine, JSError, QuickJSError);
create_exception!(_engine, MarshalError, QuickJSError);
create_exception!(_engine, InvalidHandleError, QuickJSError);

/// `Runtime::new` / `Context::full` failure mapping — used by the
/// runtime and context constructors.
pub(crate) fn map_runtime_new_error(err: Error) -> PyErr {
    QuickJSError::new_err(err.to_string())
}

/// Generic mapping for rquickjs errors raised during handle
/// restore / property access / etc. §6.7 `Error::UnrelatedRuntime`
/// is the canonical cross-context signal; it flows up to Python
/// as `InvalidHandleError` via the handle.py layer.
pub(crate) fn map_handle_error(err: Error) -> PyErr {
    QuickJSError::new_err(format!("handle restore failed: {}", err))
}

/// Return (malloc_size, malloc_limit) from the runtime's current
/// memory-usage stats, via the raw QuickJS FFI. `None` if the
/// call fails (shouldn't, but the raw API is fallible on paper).
/// Used by `js_error_from_caught` to distinguish user-threw-null
/// from degenerate-OOM — see that function for the full story.
fn runtime_memory_stats(ctx: &Ctx<'_>) -> Option<(i64, i64)> {
    use rquickjs::qjs;
    use std::mem::MaybeUninit;
    unsafe {
        let rt = qjs::JS_GetRuntime(ctx.as_raw().as_ptr());
        if rt.is_null() {
            return None;
        }
        let mut usage: MaybeUninit<qjs::JSMemoryUsage> = MaybeUninit::uninit();
        qjs::JS_ComputeMemoryUsage(rt, usage.as_mut_ptr());
        let usage = usage.assume_init();
        Some((usage.malloc_size as i64, usage.malloc_limit as i64))
    }
}

/// Convert a caught JS exception into a `JSError` PyErr carrying
/// (name, message, stack). Used by every sync eval / call site.
/// The Python layer then promotes to the right public error class
/// per §10.4 via `_classify_jserror`.
pub(crate) fn js_error_from_caught<'js>(
    _ctx: &Ctx<'js>,
    caught: CaughtError<'js>,
) -> PyErr {
    match caught {
        CaughtError::Exception(exc) => {
            let name = exc
                .as_object()
                .get::<_, String>(PredefinedAtom::Name)
                .unwrap_or_else(|_| "Error".to_string());
            let message = exc.message().unwrap_or_default();
            let stack = exc.stack();
            JSError::new_err((name, message, stack))
        }
        CaughtError::Value(val) => {
            // Degenerate-OOM detection: when QuickJS runs out of
            // memory mid-execution it *tries* to throw
            // InternalError("out of memory"), but constructing the
            // exception object itself needs memory. If that fails,
            // QuickJS ends up with a null/undefined caught value —
            // which rquickjs surfaces as CaughtError::Value. To
            // distinguish a "user threw null" from "OOM threw
            // null", peek at the runtime's malloc stats via the
            // raw FFI; if malloc_size is close to malloc_limit the
            // thrown null is almost certainly OOM and we
            // synthesize the canonical tuple so the Python
            // classifier routes to MemoryLimitError. Seen on
            // macOS CI with 64 MB limit + runaway array.push loop;
            // local-M1 gets a clean "out of memory" throw for the
            // same workload. Different manifestation, same root
            // cause.
            if val.is_null() || val.is_undefined() {
                if let Some((size, limit)) = runtime_memory_stats(_ctx) {
                    // 90% of the limit as the OOM-proximity
                    // threshold: QuickJS may free some memory
                    // during unwind before we read the stats, so a
                    // strict `>= limit` check would miss cases
                    // where the rollback dropped usage slightly
                    // below the limit. 90% comfortably separates
                    // the OOM window from the user-threw-null case
                    // (where usage is near zero).
                    if limit > 0 && size * 10 >= limit * 9 {
                        return JSError::new_err((
                            "InternalError".to_string(),
                            "out of memory".to_string(),
                            None::<String>,
                        ));
                    }
                }
            }
            // §10.1: non-Error throws (`throw 42`, `throw 'x'`)
            // coerce to JSError(name="Error", message=ToString(val)).
            let message: String = Coerced::<String>::from_js(_ctx, val)
                .map(|c| c.0)
                .unwrap_or_else(|_| "<unprintable>".to_string());
            JSError::new_err(("Error".to_string(), message, None::<String>))
        }
        CaughtError::Error(e) => QuickJSError::new_err(e.to_string()),
    }
}
