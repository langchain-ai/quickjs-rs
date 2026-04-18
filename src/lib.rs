//! quickjs_rs._engine — PyO3 extension wrapping rquickjs.
//!
//! See spec/implementation.md §6 for the full Rust-layer spec. The
//! code is split into focused modules:
//!
//!   * `errors`     — §10 exception classes + rquickjs→PyErr mapping
//!   * `reentrance` — §6.7 thread-local active_ctx slot + helper
//!   * `marshal`    — §6.6 JS↔Python value conversion + Undefined
//!   * `host_fn`    — §6.5 sync + async host-function trampolines
//!   * `runtime`    — §6.2 QjsRuntime pyclass
//!   * `context`    — §6.3 QjsContext pyclass (the biggest)
//!   * `handle`     — §6.4 QjsHandle pyclass
//!   * `modules`    — §5.2 ES-module store, resolver, loader
//!
//! This file just wires everything into the `_engine` Python module.

use pyo3::prelude::*;

mod context;
mod errors;
mod handle;
mod host_fn;
mod marshal;
mod modules;
mod reentrance;
mod runtime;

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
    m.add("InvalidHandleError", m.py().get_type::<InvalidHandleError>())?;
    Ok(())
}
