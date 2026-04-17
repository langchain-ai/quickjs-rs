use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyBytes, PyByteArray, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use rquickjs::{
    atom::PredefinedAtom,
    context::EvalOptions,
    convert::{Coerced, FromJs},
    object::Filter,
    runtime::InterruptHandler,
    Array, BigInt, CatchResultExt, CaughtError, Context, Ctx, Error, Function, Object, Runtime,
    String as JsString, Type, TypedArray, Value,
};

// §10 exception hierarchy. PyO3 needs native classes to raise; the
// Python side re-exports these names from `quickjs_rs.errors`.
create_exception!(_engine, QuickJSError, PyException);
create_exception!(_engine, JSError, QuickJSError);
create_exception!(_engine, MarshalError, QuickJSError);

// §8 invariant: depth cap for recursive marshaling. Matches v0.2's
// bridge (cycle detection via depth limit, no ref tracking). Cycles
// go around the guard only by being genuinely deeper than 128 — at
// which point the MarshalError is the right signal regardless.
const MAX_MARSHAL_DEPTH: u32 = 128;

/// Sentinel for JS `undefined` when it appears nested inside a
/// structure. §6.6: distinct from `None` so a future
/// ``preserve_undefined=True`` mode can keep the distinction at the
/// root too. Equality is instance-agnostic: any two Undefined
/// objects compare equal, so Rust creating fresh instances is fine.
#[pyclass(module = "quickjs_rs._engine", frozen, eq, hash, str = "Undefined", skip_from_py_object)]
#[derive(PartialEq, Eq, Hash, Clone)]
struct Undefined;

#[pymethods]
impl Undefined {
    #[new]
    fn new() -> Self {
        Undefined
    }

    fn __repr__(&self) -> &'static str {
        "Undefined"
    }

    fn __bool__(&self) -> bool {
        false
    }
}

fn map_runtime_new_error(err: Error) -> PyErr {
    QuickJSError::new_err(err.to_string())
}

fn depth_error() -> PyErr {
    MarshalError::new_err(format!(
        "recursion limit of {} exceeded while marshaling (cycle or deeply nested structure)",
        MAX_MARSHAL_DEPTH
    ))
}

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
            let message: String = Coerced::<String>::from_js(_ctx, val)
                .map(|c| c.0)
                .unwrap_or_else(|_| "<unprintable>".to_string());
            JSError::new_err(("Error".to_string(), message, None::<String>))
        }
        CaughtError::Error(e) => QuickJSError::new_err(e.to_string()),
    }
}

/// Marshal a JS value to a Python object per §6.6. `depth` is the
/// current recursion level; start at 0.
fn js_value_to_py<'js>(py: Python<'_>, val: &Value<'js>, depth: u32) -> PyResult<Py<PyAny>> {
    if depth >= MAX_MARSHAL_DEPTH {
        return Err(depth_error());
    }

    match val.type_of() {
        Type::Null => Ok(py.None()),
        Type::Undefined | Type::Uninitialized => {
            if depth == 0 {
                // Root of ctx.eval: coerce to None per §6.6
                // (preserve_undefined=False is the default; a future
                // preserve_undefined=True flag flips this).
                Ok(py.None())
            } else {
                // Nested: keep the Undefined sentinel so callers can
                // distinguish `[1, undefined, 2]` from `[1, null, 2]`.
                Ok(Undefined.into_pyobject(py)?.unbind().into_any())
            }
        }
        Type::Bool => Ok(val
            .as_bool()
            .expect("Type::Bool has as_bool")
            .into_pyobject(py)?
            .to_owned()
            .unbind()
            .into_any()),
        Type::Int => {
            let n = val.as_int().expect("Type::Int has as_int");
            Ok(n.into_pyobject(py)?.unbind().into_any())
        }
        Type::Float => {
            let n = val.as_float().expect("Type::Float has as_float");
            // §6.6: "PyFloat (or PyInt if integer-valued)" — preserve
            // narrowing so `1 + 2` returns int 3, not float 3.0.
            if n.is_finite() && n.fract() == 0.0 && n >= i64::MIN as f64 && n <= i64::MAX as f64 {
                Ok((n as i64).into_pyobject(py)?.unbind().into_any())
            } else {
                Ok(PyFloat::new(py, n).unbind().into_any())
            }
        }
        Type::String => {
            let s = val.as_string().expect("Type::String has as_string");
            let rust_str = s.to_string().map_err(|e| {
                MarshalError::new_err(format!("failed to decode JS string: {}", e))
            })?;
            Ok(PyString::new(py, &rust_str).unbind().into_any())
        }
        Type::BigInt => {
            // §6.6: BigInt → Python int. Python int is arbitrary
            // precision, so there's no upper bound. Go via the
            // JS-side decimal-string representation — i64 wouldn't
            // cover 2^63+ values.
            let bi = val.as_big_int().expect("Type::BigInt has as_big_int");
            // Coerced::<String>::from_js uses JS ToString, which for
            // BigInt is the full decimal representation.
            let s: String = Coerced::<String>::from_js(val.ctx(), val.clone())
                .map(|c| c.0)
                .map_err(|e| {
                    MarshalError::new_err(format!("failed to stringify BigInt: {}", e))
                })?;
            let _ = bi; // keep the variable for clarity; as_big_int is the type gate
            // Parse the decimal string into a Python int.
            py.import("builtins")?
                .getattr("int")?
                .call1((s,))
                .map(|b| b.unbind())
        }
        Type::Array => {
            let arr = val.as_array().expect("Type::Array has as_array");
            let list = PyList::empty(py);
            for item in arr.iter::<Value<'js>>() {
                let item = item.map_err(|e| {
                    MarshalError::new_err(format!("array element read failed: {}", e))
                })?;
                list.append(js_value_to_py(py, &item, depth + 1)?)?;
            }
            Ok(list.unbind().into_any())
        }
        Type::Object => {
            let obj = val.as_object().expect("Type::Object has as_object");
            // Uint8Array check must come before the generic object
            // path — otherwise a Uint8Array would marshal as
            // {"0": 1, "1": 2, ...}.
            if obj.is_typed_array::<u8>() {
                let typed = TypedArray::<u8>::from_object(obj.clone()).map_err(|e| {
                    MarshalError::new_err(format!("Uint8Array extraction failed: {}", e))
                })?;
                let bytes = typed.as_bytes().ok_or_else(|| {
                    MarshalError::new_err("Uint8Array is detached; cannot marshal")
                })?;
                return Ok(PyBytes::new(py, bytes).unbind().into_any());
            }
            let dict = PyDict::new(py);
            // Enumerable string-keyed own properties, preserving
            // insertion order (QuickJS's iterator does this; §6.6
            // "Object → PyDict with string keys").
            for entry in obj.own_props::<String, Value<'js>>(Filter::default()) {
                let (key, value) = entry.map_err(|e| {
                    MarshalError::new_err(format!("object entry read failed: {}", e))
                })?;
                dict.set_item(key, js_value_to_py(py, &value, depth + 1)?)?;
            }
            Ok(dict.unbind().into_any())
        }
        // §6.6: functions/symbols raise MarshalError unless
        // allow_opaque=true (which is the QjsHandle path; sync eval
        // result rejects them).
        Type::Function | Type::Constructor => Err(MarshalError::new_err(
            "functions cannot be marshaled to Python (use eval_handle for an opaque handle)",
        )),
        Type::Symbol => Err(MarshalError::new_err(
            "symbols cannot be marshaled to Python",
        )),
        Type::Exception | Type::Promise | Type::Proxy | Type::Module | Type::Unknown => {
            Err(MarshalError::new_err(format!(
                "JS value of type {} cannot be marshaled to Python",
                val.type_of()
            )))
        }
    }
}

/// Marshal a Python object to a JS value per §6.6. Step 4 (globals
/// write) and step 5 (host function args/returns) are the first
/// call sites.
#[allow(dead_code)]
fn py_to_js_value<'js>(
    ctx: &Ctx<'js>,
    py_val: &Bound<'_, PyAny>,
    depth: u32,
) -> PyResult<Value<'js>> {
    if depth >= MAX_MARSHAL_DEPTH {
        return Err(depth_error());
    }

    let py = py_val.py();

    // None → JS null. Undefined sentinel → JS undefined.
    if py_val.is_none() {
        return Ok(Value::new_null(ctx.clone()));
    }
    if py_val.is_exact_instance_of::<Undefined>() || py_val.is_instance_of::<Undefined>() {
        return Ok(Value::new_undefined(ctx.clone()));
    }

    // bool check before int — Python bool is a subclass of int. If we
    // checked int first, True would marshal as JS number 1 instead of
    // JS true.
    if let Ok(b) = py_val.cast::<PyBool>() {
        return Ok(Value::new_bool(ctx.clone(), b.is_true()));
    }

    // int. Values in ±2^53 go as JS number (float64-safe integer
    // range). Outside that, go as BigInt via decimal-string through
    // the globalThis.BigInt constructor — rquickjs::BigInt::from_i64
    // only covers ±2^63 and misses truly large Python ints.
    if let Ok(i) = py_val.cast::<PyInt>() {
        // Extract as i64 first; fall through to BigInt path on overflow.
        match i.extract::<i64>() {
            Ok(n) if (-(1i64 << 53)..=(1i64 << 53)).contains(&n) => {
                return Ok(Value::new_number(ctx.clone(), n as f64));
            }
            Ok(n) => {
                // Fits in i64 but outside safe-integer range: BigInt.
                let bi = BigInt::from_i64(ctx.clone(), n).map_err(|e| {
                    MarshalError::new_err(format!("BigInt::from_i64 failed: {}", e))
                })?;
                return Ok(bi.into_value());
            }
            Err(_) => {
                // Larger than i64: use globalThis.BigInt(str) to construct.
                let s: String = i.str()?.to_str()?.to_string();
                let bigint_fn: Function<'js> = ctx
                    .globals()
                    .get("BigInt")
                    .map_err(|e| MarshalError::new_err(format!("BigInt constructor unavailable: {}", e)))?;
                let js_str = JsString::from_str(ctx.clone(), &s).map_err(|e| {
                    MarshalError::new_err(format!("failed to build JS string: {}", e))
                })?;
                let result: Value<'js> = bigint_fn.call((js_str,)).map_err(|e| {
                    MarshalError::new_err(format!("BigInt({}) failed: {}", s, e))
                })?;
                return Ok(result);
            }
        }
    }

    // float
    if let Ok(f) = py_val.cast::<PyFloat>() {
        return Ok(Value::new_number(ctx.clone(), f.extract::<f64>()?));
    }

    // str
    if let Ok(s) = py_val.cast::<PyString>() {
        let rust_str = s.to_str()?;
        let js_str = JsString::from_str(ctx.clone(), rust_str).map_err(|e| {
            MarshalError::new_err(format!("failed to build JS string: {}", e))
        })?;
        return Ok(js_str.into_value());
    }

    // bytes / bytearray → Uint8Array
    if let Ok(b) = py_val.cast::<PyBytes>() {
        let bytes = b.as_bytes();
        let ta = TypedArray::<u8>::new_copy(ctx.clone(), bytes).map_err(|e| {
            MarshalError::new_err(format!("Uint8Array allocation failed: {}", e))
        })?;
        return Ok(ta.into_value());
    }
    if let Ok(b) = py_val.cast::<PyByteArray>() {
        // Safety: we copy out immediately via to_vec before any other
        // Python code runs, so buffer mutation can't happen.
        let bytes = unsafe { b.as_bytes() }.to_vec();
        let ta = TypedArray::<u8>::new_copy(ctx.clone(), &bytes[..]).map_err(|e| {
            MarshalError::new_err(format!("Uint8Array allocation failed: {}", e))
        })?;
        return Ok(ta.into_value());
    }

    // list / tuple → JS Array
    if let Ok(lst) = py_val.cast::<PyList>() {
        let arr = Array::new(ctx.clone()).map_err(|e| {
            MarshalError::new_err(format!("JS Array allocation failed: {}", e))
        })?;
        for (i, item) in lst.iter().enumerate() {
            let v = py_to_js_value(ctx, &item, depth + 1)?;
            arr.set(i, v).map_err(|e| {
                MarshalError::new_err(format!("Array set failed at {}: {}", i, e))
            })?;
        }
        return Ok(arr.into_value());
    }
    if let Ok(tup) = py_val.cast::<PyTuple>() {
        let arr = Array::new(ctx.clone()).map_err(|e| {
            MarshalError::new_err(format!("JS Array allocation failed: {}", e))
        })?;
        for (i, item) in tup.iter().enumerate() {
            let v = py_to_js_value(ctx, &item, depth + 1)?;
            arr.set(i, v).map_err(|e| {
                MarshalError::new_err(format!("Array set failed at {}: {}", i, e))
            })?;
        }
        return Ok(arr.into_value());
    }

    // dict with string keys → JS Object. Non-string keys raise
    // MarshalError because JS own-property keys are strings or
    // symbols; we don't support symbol keys on the write path.
    if let Ok(d) = py_val.cast::<PyDict>() {
        let obj = Object::new(ctx.clone()).map_err(|e| {
            MarshalError::new_err(format!("JS Object allocation failed: {}", e))
        })?;
        for (key, value) in d.iter() {
            let key_str = key
                .cast::<PyString>()
                .map_err(|_| {
                    MarshalError::new_err(format!(
                        "dict keys must be strings for JS marshaling; got {}",
                        key.get_type().name().map(|s| s.to_string()).unwrap_or_else(|_| "?".into())
                    ))
                })?
                .to_str()?;
            let v = py_to_js_value(ctx, &value, depth + 1)?;
            obj.set(key_str, v).map_err(|e| {
                MarshalError::new_err(format!("Object set failed for key {:?}: {}", key_str, e))
            })?;
        }
        let _ = py;
        return Ok(obj.into_value());
    }

    let type_name = py_val
        .get_type()
        .name()
        .map(|s| s.to_string())
        .unwrap_or_else(|_| "?".into());
    Err(MarshalError::new_err(format!(
        "Python value of type {} cannot be marshaled to JS",
        type_name
    )))
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
            let result: Result<Value<'_>, CaughtError<'_>> =
                ctx.eval_with_options::<Value<'_>, _>(code, options).catch(&ctx);
            match result {
                Ok(val) => js_value_to_py(py, &val, 0),
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
    m.add_class::<Undefined>()?;
    m.add("UNDEFINED", Undefined.into_pyobject(m.py())?)?;
    m.add("QuickJSError", m.py().get_type::<QuickJSError>())?;
    m.add("JSError", m.py().get_type::<JSError>())?;
    m.add("MarshalError", m.py().get_type::<MarshalError>())?;
    Ok(())
}
