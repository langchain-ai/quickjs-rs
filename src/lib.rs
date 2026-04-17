use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyFloat, PyInt};
use rquickjs::{
    atom::PredefinedAtom,
    context::EvalOptions,
    convert::{Coerced, FromJs},
    runtime::InterruptHandler,
    CatchResultExt, CaughtError, Context, Ctx, Error, Runtime, Type, Value,
};

// §10 exception hierarchy. PyO3 needs native classes to raise; the
// Python side re-exports these names from `quickjs_rs.errors`. Each
// step adds the classes its surface area needs.
create_exception!(_engine, QuickJSError, PyException);
create_exception!(_engine, JSError, QuickJSError);
create_exception!(_engine, MarshalError, QuickJSError);

fn map_runtime_new_error(err: Error) -> PyErr {
    QuickJSError::new_err(err.to_string())
}

/// Build a `JSError` PyErr from a caught JS exception. Lifetime
/// parameter ties `Ctx` and `CaughtError` to the same `'js` scope
/// — both were created inside the same `Context::with` closure.
fn js_error_from_caught<'js>(_ctx: &Ctx<'js>, caught: CaughtError<'js>) -> PyErr {
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
            // `throw 42` / `throw "oops"` — §10.1: non-Error throws
            // coerce to JSError(name="Error", message=ToString(val)).
            // Use rquickjs's Coerced<String> to get JS's ToString.
            let message: String = Coerced::<String>::from_js(_ctx, val)
                .map(|c| c.0)
                .unwrap_or_else(|_| "<unprintable>".to_string());
            JSError::new_err(("Error".to_string(), message, None::<String>))
        }
        CaughtError::Error(e) => QuickJSError::new_err(e.to_string()),
    }
}

/// Marshal a JS value to a Python object. Step 2 handles numbers
/// only — everything else becomes a MarshalError. Step 3 widens.
fn js_value_to_py(py: Python<'_>, val: &Value<'_>) -> PyResult<Py<PyAny>> {
    match val.type_of() {
        Type::Int => {
            let n = val.as_int().expect("Type::Int has as_int");
            Ok(n.into_pyobject(py)?.unbind().into_any())
        }
        Type::Float => {
            let n = val.as_float().expect("Type::Float has as_float");
            // §8 invariant: "Numbers are always float64 on the wire."
            // But §6.6 says PyFloat "or PyInt if integer-valued" — the
            // Python user sees 1+2 == 3 (int), not 3.0. Preserve that
            // narrowing for integer-valued floats that fit in i64.
            if n.is_finite() && n.fract() == 0.0 && n >= i64::MIN as f64 && n <= i64::MAX as f64 {
                Ok((n as i64).into_pyobject(py)?.unbind().into_any())
            } else {
                Ok(PyFloat::new(py, n).unbind().into_any())
            }
        }
        other => Err(MarshalError::new_err(format!(
            "JS value of type {} cannot be marshaled to Python yet \
             (step 2 only handles numbers; step 3 widens)",
            other
        ))),
    }
}

#[pyclass(module = "quickjs_rs._engine", unsendable)]
struct QjsRuntime {
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
    fn runtime(&self) -> PyResult<&Runtime> {
        self.inner
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("runtime is closed"))
    }
}

#[pyclass(module = "quickjs_rs._engine", unsendable)]
struct QjsContext {
    inner: Option<Context>,
}

#[pymethods]
impl QjsContext {
    #[new]
    fn new(runtime: &QjsRuntime) -> PyResult<Self> {
        let rt = runtime.runtime()?;
        let ctx = Context::full(rt).map_err(map_runtime_new_error)?;
        Ok(Self { inner: Some(ctx) })
    }

    /// Evaluate JS source in global (script) mode. `module` enables
    /// ES-module parsing; `strict` forces strict mode; `filename`
    /// labels the script for backtraces.
    #[pyo3(signature = (code, *, module=false, strict=false, filename="<eval>"))]
    fn eval(
        &self,
        py: Python<'_>,
        code: &str,
        module: bool,
        strict: bool,
        filename: &str,
    ) -> PyResult<Py<PyAny>> {
        let ctx = self.context()?;
        ctx.with(|ctx| {
            let mut options = EvalOptions::default();
            options.global = !module;
            options.strict = strict;
            options.filename = Some(filename.to_string());
            // Run eval and catch any JS exception into a structured form.
            let result: Result<Value<'_>, CaughtError<'_>> =
                ctx.eval_with_options::<Value<'_>, _>(code, options).catch(&ctx);
            match result {
                Ok(val) => js_value_to_py(py, &val),
                Err(caught) => Err(js_error_from_caught(&ctx, caught)),
            }
        })
    }

    fn close(&mut self) -> PyResult<()> {
        self.inner = None;
        Ok(())
    }

    fn is_closed(&self) -> bool {
        self.inner.is_none()
    }
}

impl QjsContext {
    fn context(&self) -> PyResult<&Context> {
        self.inner
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("context is closed"))
    }
}

#[pymodule]
fn _engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<QjsRuntime>()?;
    m.add_class::<QjsContext>()?;
    m.add("QuickJSError", m.py().get_type::<QuickJSError>())?;
    m.add("JSError", m.py().get_type::<JSError>())?;
    m.add("MarshalError", m.py().get_type::<MarshalError>())?;
    // Silence PyFloat/PyInt warnings — they're used implicitly via into_pyobject.
    let _ = (m.py().get_type::<PyFloat>(), m.py().get_type::<PyInt>());
    Ok(())
}
