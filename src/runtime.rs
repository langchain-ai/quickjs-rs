//! §6.2 QjsRuntime pyclass — wraps rquickjs::Runtime.

use pyo3::prelude::*;
use pyo3::types::PyAny;
use rquickjs::{runtime::InterruptHandler, Runtime};

use crate::errors::{map_runtime_new_error, QuickJSError};
use crate::modules::{StoreHandle, StoreLoader, StoreResolver};

#[pyclass(module = "quickjs_rs._engine", unsendable)]
pub(crate) struct QjsRuntime {
    inner: Option<Runtime>,
    /// §5.2: module registry. Created lazily on the first install
    /// call so runtimes that never use modules pay nothing. Once
    /// created, the handle stays — `rt.set_loader` consumed its
    /// own clones of the Resolver and Loader, but they share the
    /// same `Rc<RefCell<ModuleStore>>` backing so subsequent
    /// installs reach them through this clone.
    module_store: Option<StoreHandle>,
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
        Ok(Self {
            inner: Some(rt),
            module_store: None,
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

    /// §5.3: register a str-valued scope entry. Called from
    /// Python's recursive Context.install walk for each str entry.
    ///
    ///   * `scope_path`: canonical path of the containing scope
    ///     ("" for the root, or a '/'-joined chain like
    ///     "@agent/fs" or "@agent/fs/@peer").
    ///   * `key`: the dict key (a POSIX path within the scope,
    ///     which may itself contain '/').
    ///   * `canonical_path`: the joined scope+key path used both
    ///     as the source-map key and as the module name QuickJS
    ///     sees when it asks the loader to materialize the module.
    ///   * `source`: the JS source text.
    fn add_module_source(
        &mut self,
        scope_path: &str,
        key: &str,
        canonical_path: &str,
        source: &str,
    ) -> PyResult<()> {
        let handle = self.ensure_module_store()?;
        handle.with_mut(|store| {
            store.add_source(scope_path, key, canonical_path, source);
        });
        Ok(())
    }

    /// §5.3: declare that `child_key` inside `scope_path` is a
    /// ModuleScope (bare-specifier child). Called by the Python
    /// install walk for each ModuleScope-valued entry before
    /// recursing into it.
    fn register_subscope(&mut self, scope_path: &str, child_key: &str) -> PyResult<()> {
        let handle = self.ensure_module_store()?;
        handle.with_mut(|store| {
            store.register_subscope(scope_path, child_key);
        });
        Ok(())
    }

    fn close(&mut self) -> PyResult<()> {
        self.inner = None;
        self.module_store = None;
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

    /// Lazily create the module store and install the rquickjs
    /// Resolver + Loader on first use. Subsequent calls return
    /// the existing handle.
    fn ensure_module_store(&mut self) -> PyResult<&StoreHandle> {
        if self.module_store.is_none() {
            let handle = StoreHandle::new();
            let rt = self
                .inner
                .as_ref()
                .ok_or_else(|| QuickJSError::new_err("runtime is closed"))?;
            rt.set_loader(
                StoreResolver(handle.clone()),
                StoreLoader(handle.clone()),
            );
            self.module_store = Some(handle);
        }
        Ok(self.module_store.as_ref().unwrap())
    }
}
