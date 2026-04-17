//! §6.2 QjsRuntime pyclass — wraps rquickjs::Runtime.

use pyo3::prelude::*;
use pyo3::types::PyAny;
use rquickjs::{runtime::InterruptHandler, Runtime};

use crate::errors::{map_runtime_new_error, QuickJSError};

#[pyclass(module = "quickjs_rs._engine", unsendable)]
pub(crate) struct QjsRuntime {
    inner: Option<Runtime>,
}

#[pymethods]
impl QjsRuntime {
    #[new]
    #[pyo3(signature = (*, memory_limit=None, stack_limit=None))]
    fn new(memory_limit: Option<usize>, stack_limit: Option<usize>) -> PyResult<Self> {
        let rt = Runtime::new().map_err(map_runtime_new_error)?;
        if let Some(limit) = memory_limit {
            rt.set_memory_limit(limit);
        }
        if let Some(limit) = stack_limit {
            rt.set_max_stack_size(limit);
        }
        Ok(Self { inner: Some(rt) })
    }

    fn set_interrupt_handler(&self, handler: Py<PyAny>) -> PyResult<()> {
        let rt = self.runtime()?;
        let cb: InterruptHandler = Box::new(move || {
            Python::attach(|py| match handler.bind(py).call0() {
                Ok(result) => result.is_truthy().unwrap_or(true),
                Err(e) => {
                    e.print_and_set_sys_last_vars(py);
                    true
                }
            })
        });
        rt.set_interrupt_handler(Some(cb));
        Ok(())
    }

    fn clear_interrupt_handler(&self) -> PyResult<()> {
        self.runtime()?.set_interrupt_handler(None);
        Ok(())
    }

    fn close(&mut self) -> PyResult<()> {
        self.inner = None;
        Ok(())
    }

    fn is_closed(&self) -> bool {
        self.inner.is_none()
    }
}

impl QjsRuntime {
    pub(crate) fn runtime(&self) -> PyResult<&Runtime> {
        self.inner
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("runtime is closed"))
    }
}
