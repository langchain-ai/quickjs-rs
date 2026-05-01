//! quickjs_rs._engine — PyO3 extension wrapping rquickjs.
//!
//! The code is split into modules:
//!
//!   * `errors`     — exception classes + rquickjs→PyErr mapping
//!   * `reentrance` — thread-local active_ctx slot + helper
//!   * `marshal`    — JS↔Python value conversion + Undefined
//!   * `host_fn`    — sync + async host-function trampolines
//!   * `runtime`    — QjsRuntime pyclass
//!   * `context`    — QjsContext pyclass (the biggest)
//!   * `handle`     — QjsHandle pyclass
//!   * `modules`    — ES-module store, resolver, loader
//!
//! This file just wires everything into the `_engine` Python module.

use pyo3::prelude::*;

mod ast;
mod context;
mod errors;
mod handle;
mod host_fn;
mod marshal;
mod modules;
mod reentrance;
mod runtime;
mod snapshot;
mod transpile;

use crate::context::QjsContext;
use crate::errors::{InvalidHandleError, JSError, MarshalError, QuickJSError};
use crate::handle::QjsHandle;
use crate::marshal::Undefined;
use crate::runtime::QjsRuntime;

#[pymodule]
fn _engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<QjsRuntime>()?;
    m.add_class::<QjsContext>()?;
    m.add_class::<QjsHandle>()?;
    m.add_class::<Undefined>()?;
    m.add("UNDEFINED", Undefined.into_pyobject(m.py())?)?;
    m.add("QuickJSError", m.py().get_type::<QuickJSError>())?;
    m.add("JSError", m.py().get_type::<JSError>())?;
    m.add("MarshalError", m.py().get_type::<MarshalError>())?;
    m.add(
        "InvalidHandleError",
        m.py().get_type::<InvalidHandleError>(),
    )?;
    Ok(())
}
