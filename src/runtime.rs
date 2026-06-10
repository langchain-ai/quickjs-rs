//! QjsRuntime pyclass — wraps rquickjs::Runtime.

use pyo3::prelude::*;
use pyo3::types::PyAny;
use pyo3::types::PyDict;
use rquickjs::{runtime::InterruptHandler, Runtime};

use crate::errors::{map_runtime_new_error, QuickJSError};
use crate::modules::{StoreHandle, StoreLoader, StoreResolver};

#[pyclass(module = "quickjs_rs._engine", unsendable)]
pub(crate) struct QjsRuntime {
    inner: Option<Runtime>,
    module_store: StoreHandle,
}

#[pymethods]
impl QjsRuntime {
    #[new]
    #[pyo3(signature = (*, memory_limit=None, stack_limit=None))]
    fn new(memory_limit: Option<usize>, stack_limit: Option<usize>) -> PyResult<Self> {
        let rt = Runtime::new().map_err(map_runtime_new_error)?;
        let module_store = StoreHandle::new();
        if let Some(limit) = memory_limit {
            rt.set_memory_limit(limit);
        }
        if let Some(limit) = stack_limit {
            rt.set_max_stack_size(limit);
        }
        // Install the module resolver/loader once per runtime.
        rt.set_loader(
            StoreResolver(module_store.clone()),
            StoreLoader(module_store.clone()),
        );
        Ok(Self {
            inner: Some(rt),
            module_store,
        })
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

    /// Run QuickJS cycle GC for this runtime.
    fn run_gc(&self) -> PyResult<()> {
        self.runtime()?.run_gc();
        Ok(())
    }

    /// Snapshot QuickJS runtime memory counters from
    /// JS_ComputeMemoryUsage.
    fn memory_usage(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let usage = self.runtime()?.memory_usage();
        let out = PyDict::new(py);
        out.set_item("malloc_size", usage.malloc_size)?;
        out.set_item("malloc_limit", usage.malloc_limit)?;
        out.set_item("memory_used_size", usage.memory_used_size)?;
        out.set_item("malloc_count", usage.malloc_count)?;
        out.set_item("memory_used_count", usage.memory_used_count)?;
        out.set_item("atom_count", usage.atom_count)?;
        out.set_item("atom_size", usage.atom_size)?;
        out.set_item("str_count", usage.str_count)?;
        out.set_item("str_size", usage.str_size)?;
        out.set_item("obj_count", usage.obj_count)?;
        out.set_item("obj_size", usage.obj_size)?;
        out.set_item("prop_count", usage.prop_count)?;
        out.set_item("prop_size", usage.prop_size)?;
        out.set_item("shape_count", usage.shape_count)?;
        out.set_item("shape_size", usage.shape_size)?;
        out.set_item("js_func_count", usage.js_func_count)?;
        out.set_item("js_func_size", usage.js_func_size)?;
        out.set_item("js_func_code_size", usage.js_func_code_size)?;
        out.set_item("js_func_pc2line_count", usage.js_func_pc2line_count)?;
        out.set_item("js_func_pc2line_size", usage.js_func_pc2line_size)?;
        out.set_item("c_func_count", usage.c_func_count)?;
        out.set_item("array_count", usage.array_count)?;
        out.set_item("fast_array_count", usage.fast_array_count)?;
        out.set_item("fast_array_elements", usage.fast_array_elements)?;
        out.set_item("binary_object_count", usage.binary_object_count)?;
        out.set_item("binary_object_size", usage.binary_object_size)?;
        Ok(out.unbind())
    }

    /// Configure (or clear) the runtime's dynamic import fallback.
    ///
    /// Signature:
    ///   handler(requested_key, referrer, specifier)
    ///     -> str | None
    ///
    /// `referrer` is None for top-level eval.
    #[pyo3(signature = (handler=None))]
    fn set_import_handler(&self, handler: Option<Py<PyAny>>) -> PyResult<()> {
        let _ = self.runtime()?;
        self.module_store.set_source_handler(handler);
        Ok(())
    }

    fn close(&mut self) -> PyResult<()> {
        self.module_store.with_mut(|store| {
            store.resolved_sources.clear();
            store.dynamic_source_handler = None;
        });
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
