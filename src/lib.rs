use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyAny;
use rquickjs::{runtime::InterruptHandler, Runtime};

// §10 exception hierarchy. The Python side re-exports these names
// from `quickjs_rs.errors`; PyO3 needs native classes to raise.
// For phase 1 step 1 only `QuickJSError` is required — more land as
// later steps need them.
create_exception!(_engine, QuickJSError, PyException);

fn map_rquickjs_error(err: rquickjs::Error) -> PyErr {
    QuickJSError::new_err(err.to_string())
}

#[pyclass(module = "quickjs_rs._engine", unsendable)]
struct QjsRuntime {
    // Option so close() can consume the inner Runtime before final Drop.
    inner: Option<Runtime>,
}

#[pymethods]
impl QjsRuntime {
    #[new]
    #[pyo3(signature = (*, memory_limit=None, stack_limit=None))]
    fn new(memory_limit: Option<usize>, stack_limit: Option<usize>) -> PyResult<Self> {
        let rt = Runtime::new().map_err(map_rquickjs_error)?;
        if let Some(limit) = memory_limit {
            rt.set_memory_limit(limit);
        }
        if let Some(limit) = stack_limit {
            rt.set_max_stack_size(limit);
        }
        Ok(Self { inner: Some(rt) })
    }

    /// Install a Python callable as the interrupt handler. The callable
    /// takes no args and returns a truthy value to abort JS execution.
    /// §6.7: called with the GIL held, on the same thread eval runs on
    /// (rquickjs's `InterruptHandler` is `FnMut + 'static`, not Send —
    /// we don't enable the `parallel` feature).
    fn set_interrupt_handler(&self, handler: Py<PyAny>) -> PyResult<()> {
        let rt = self.runtime()?;
        let cb: InterruptHandler = Box::new(move || {
            Python::attach(|py| match handler.bind(py).call0() {
                Ok(result) => result.is_truthy().unwrap_or(true),
                // A Python exception from the interrupt handler can't
                // propagate through QuickJS; treat it as "please abort"
                // so the caller sees a clean InterruptError rather than
                // hanging. The exception is printed to stderr.
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
        // Dropping the Option's Some(Runtime) invokes rquickjs's Drop,
        // which JS_FreeRuntime's the underlying QuickJS state. Idempotent.
        self.inner = None;
        Ok(())
    }

    fn is_closed(&self) -> bool {
        self.inner.is_none()
    }
}

impl QjsRuntime {
    fn runtime(&self) -> PyResult<&Runtime> {
        self.inner
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("runtime is closed"))
    }
}

#[pymodule]
fn _engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<QjsRuntime>()?;
    m.add("QuickJSError", m.py().get_type::<QuickJSError>())?;
    Ok(())
}
