//! Exception hierarchy + helpers that convert rquickjs errors
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
/// restore / property access / etc. `Error::UnrelatedRuntime`
/// is the canonical cross-context signal; it flows up to Python
/// as `InvalidHandleError` via the handle.py layer.
pub(crate) fn map_handle_error(err: Error) -> PyErr {
    QuickJSError::new_err(format!("handle restore failed: {}", err))
}

/// Convert a caught JS exception into a `JSError` PyErr carrying
/// (name, message, stack). Used by every sync eval / call site.
/// The Python layer then promotes to the right public error class
/// per via `_classify_jserror`.
pub(crate) fn js_error_from_caught<'js>(_ctx: &Ctx<'js>, caught: CaughtError<'js>) -> PyErr {
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
            // non-Error throws (`throw 42`, `throw 'x'`)
            // coerce to JSError(name="Error", message=ToString(val)).
            let message: String = Coerced::<String>::from_js(_ctx, val)
                .map(|c| c.0)
                .unwrap_or_else(|_| "<unprintable>".to_string());
            JSError::new_err(("Error".to_string(), message, None::<String>))
        }
        CaughtError::Error(e) => QuickJSError::new_err(e.to_string()),
    }
}
