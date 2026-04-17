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
    function::Constructor,
    Array, BigInt, CatchResultExt, CaughtError, Context, Ctx, Error, Function, Object, Persistent,
    Runtime, String as JsString, Type, TypedArray, Value,
};
use std::cell::RefCell;
use std::collections::HashMap;

// §10 exception hierarchy. PyO3 needs native classes to raise; the
// Python side re-exports these names from `quickjs_rs.errors`.
create_exception!(_engine, QuickJSError, PyException);
create_exception!(_engine, JSError, QuickJSError);
create_exception!(_engine, MarshalError, QuickJSError);
create_exception!(_engine, InvalidHandleError, QuickJSError);

// §8 invariant: depth cap for recursive marshaling. Matches v0.2's
// bridge (cycle detection via depth limit, no ref tracking). Cycles
// go around the guard only by being genuinely deeper than 128 — at
// which point the MarshalError is the right signal regardless.
const MAX_MARSHAL_DEPTH: u32 = 128;

// §6.7: reentrance-safe Ctx access. rquickjs's non-parallel runtime
// guard is a non-reentrant RefCell (per-runtime, not per-context),
// so nested `Context::with` from within a host-fn callback panics
// with "RefCell already borrowed". We stash the currently-active
// `Ctx<'static>` (lifetime laundered, same pattern as Persistent)
// keyed by raw JSRuntime pointer in a thread-local: any QjsHandle
// on any QjsContext sharing that runtime picks up the stashed Ctx
// during reentrance and skips the nested `with` entirely.
//
// The 'static lifetime is a lie maintained by bracketing in
// `with_active_ctx` — the slot is set on entry and cleared on exit
// via an RAII guard, so no `Ctx` handed out from it ever outlives
// its real `'js` scope.
thread_local! {
    static ACTIVE_CTX_BY_RT: RefCell<HashMap<usize, Ctx<'static>>> =
        RefCell::new(HashMap::new());
}

/// Run `f` with a live `Ctx<'js>` from `context`. If there's already
/// an active Ctx for this runtime (reentrant call from a host fn),
/// use that directly instead of re-locking via `Context::with`.
fn with_active_ctx<F, R>(context: &Context, f: F) -> PyResult<R>
where
    F: FnOnce(&Ctx<'_>) -> PyResult<R>,
{
    let rt_key = context.get_runtime_ptr() as usize;

    // Fast path: reentrant call — use the stashed Ctx. We clone it
    // out of the map so the closure can borrow the clone; the
    // stashed entry stays in place for any deeper nesting.
    let stashed: Option<Ctx<'static>> = ACTIVE_CTX_BY_RT
        .with(|cell| cell.borrow().get(&rt_key).cloned());
    if let Some(stashed) = stashed {
        // Shrink 'static back down to the local borrow. Sound because
        // we're inside the outer with's closure body right now.
        let as_short: &Ctx<'_> = unsafe {
            core::mem::transmute::<&Ctx<'static>, &Ctx<'_>>(&stashed)
        };
        return f(as_short);
    }

    // Slow path: enter Context::with, publish the Ctx into the
    // thread-local, clear on exit (incl. panic unwind via RAII).
    context.with(|ctx| {
        let static_ctx: Ctx<'static> = unsafe {
            core::mem::transmute::<Ctx<'_>, Ctx<'static>>(ctx.clone())
        };
        ACTIVE_CTX_BY_RT.with(|cell| {
            cell.borrow_mut().insert(rt_key, static_ctx);
        });
        struct Guard(usize);
        impl Drop for Guard {
            fn drop(&mut self) {
                ACTIVE_CTX_BY_RT.with(|cell| {
                    cell.borrow_mut().remove(&self.0);
                });
            }
        }
        let _guard = Guard(rt_key);
        f(&ctx)
    })
}

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

/// Marshal a Python object to a JS value per §6.6.
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
    /// §6.5 host function dispatch: a single Python-side callable
    /// that receives (fn_id, args_tuple) and returns the host fn's
    /// result. Set via set_host_call_dispatcher from the Python
    /// Context layer before any host function is registered.
    host_dispatcher: Option<Py<PyAny>>,
}

#[pymethods]
impl QjsContext {
    #[new]
    fn new(runtime: &QjsRuntime) -> PyResult<Self> {
        let rt = runtime.runtime()?;
        let ctx = Context::full(rt).map_err(map_runtime_new_error)?;
        Ok(Self {
            inner: Some(ctx),
            host_dispatcher: None,
        })
    }

    /// Install the Python-side dispatcher. The Context layer calls
    /// this once at construction time. The dispatcher signature is
    /// `dispatcher(fn_id: int, args: tuple) -> Any` — on host-fn
    /// exception it re-raises `_engine.JSError(name, message, stack)`
    /// which the Rust trampoline catches and converts to a JS throw.
    fn set_host_call_dispatcher(&mut self, dispatcher: Py<PyAny>) -> PyResult<()> {
        self.host_dispatcher = Some(dispatcher);
        Ok(())
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
        let context = self.context()?;
        with_active_ctx(context, |ctx| {
            let mut options = EvalOptions::default();
            options.global = !module;
            options.strict = strict;
            options.filename = Some(filename.to_string());
            let result: Result<Value<'_>, CaughtError<'_>> =
                ctx.eval_with_options::<Value<'_>, _>(code, options).catch(ctx);
            match result {
                Ok(val) => js_value_to_py(py, &val, 0),
                Err(caught) => Err(js_error_from_caught(ctx, caught)),
            }
        })
    }

    /// Eval that returns a QjsHandle instead of marshaling the
    /// result to Python. The handle is bound to this context —
    /// cross-context use raises InvalidHandleError at the Python
    /// layer. §6.3 eval_handle.
    #[pyo3(signature = (code, *, module=false, strict=false, filename="<eval>"))]
    fn eval_handle(
        &self,
        code: &str,
        module: bool,
        strict: bool,
        filename: &str,
    ) -> PyResult<QjsHandle> {
        let context = self.context()?.clone();
        let context_ptr = context.as_raw().as_ptr() as usize;
        let persistent = with_active_ctx(&context, |ctx| {
            let mut options = EvalOptions::default();
            options.global = !module;
            options.strict = strict;
            options.filename = Some(filename.to_string());
            let result: Result<Value<'_>, CaughtError<'_>> =
                ctx.eval_with_options::<Value<'_>, _>(code, options).catch(ctx);
            match result {
                Ok(val) => Ok(Persistent::save(ctx, val)),
                Err(caught) => Err(js_error_from_caught(ctx, caught)),
            }
        })?;
        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(persistent),
        })
    }

    fn close(&mut self) -> PyResult<()> {
        self.inner = None;
        Ok(())
    }

    fn is_closed(&self) -> bool {
        self.inner.is_none()
    }

    /// §6.5 host function registration. Installs a JS function on
    /// `globalThis` under `name` that, when called from JS, marshals
    /// args to Python via js_value_to_py, calls the host_dispatcher
    /// with (fn_id, args_tuple), and marshals the return value back
    /// to JS. On host-fn Python exception, throws a JS Error whose
    /// name/message come from the _engine.JSError re-raised by the
    /// Python dispatcher.
    ///
    /// `is_async` is ignored in step 5 (sync path only). Step 9 adds
    /// the async branch that produces a pending promise.
    #[pyo3(signature = (name, fn_id, is_async=false))]
    fn register_host_function(
        &self,
        py: Python<'_>,
        name: &str,
        fn_id: u32,
        is_async: bool,
    ) -> PyResult<()> {
        if is_async {
            return Err(QuickJSError::new_err(
                "async host functions land in step 9 (§15)",
            ));
        }
        let dispatcher = self
            .host_dispatcher
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("host_dispatcher not set"))?
            .clone_ref(py);

        let name_owned = name.to_string();
        let context = self.context()?;
        with_active_ctx(context, |ctx| {
            let js_fn = build_host_trampoline(ctx, dispatcher, fn_id, &name_owned)?;
            ctx.globals().set(name_owned.clone(), js_fn).map_err(|e| {
                QuickJSError::new_err(format!(
                    "failed to install host function {:?} on globalThis: {}",
                    name_owned, e
                ))
            })?;
            Ok(())
        })
    }

    /// Return a handle to the global object. §6.3 global_object.
    fn global_object(&self) -> PyResult<QjsHandle> {
        let ctx_owner = self.context()?.clone();
        let context_ptr = ctx_owner.as_raw().as_ptr() as usize;
        let persistent = with_active_ctx(&ctx_owner, |ctx| {
            let globals = ctx.globals();
            Ok(Persistent::save(ctx, globals.into_value()))
        })?;
        Ok(QjsHandle {
            context: Some(ctx_owner),
            context_ptr,
            persistent: Some(persistent),
        })
    }
}

impl QjsContext {
    fn context(&self) -> PyResult<&Context> {
        self.inner
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("context is closed"))
    }
}

/// Holds a `Persistent<Value>` and the pieces needed to restore it:
/// a Context clone, plus the raw context pointer for cross-context
/// identity checks. §6.4 / §6.7.
#[pyclass(module = "quickjs_rs._engine", unsendable)]
struct QjsHandle {
    context: Option<Context>,
    /// Raw JSContext pointer for cross-context identity checks.
    /// Populated at construction and never rewritten — stable for the
    /// handle's lifetime.
    context_ptr: usize,
    persistent: Option<Persistent<Value<'static>>>,
}

#[pymethods]
impl QjsHandle {
    /// Raw pointer of the context that created this handle. The
    /// Python Handle uses this to enforce the cross-context guard
    /// (§6.7: "Handles are bound to their creating context").
    #[getter]
    fn context_id(&self) -> usize {
        self.context_ptr
    }

    /// Structural type tag — "object", "array", "function", "null",
    /// "undefined", "boolean", "number", "bigint", "string",
    /// "symbol". Maps rquickjs's internal Type enum to the strings
    /// the Python API (§7.2 Handle.type_of) exposes.
    #[getter]
    fn type_of(&self) -> PyResult<String> {
        self.with_value(|_ctx, val| Ok(type_name_of(val.type_of())))
    }

    fn is_promise(&self) -> PyResult<bool> {
        self.with_value(|_ctx, val| Ok(val.is_promise()))
    }

    /// Read a property by string key, returning a QjsHandle for the
    /// resulting value. Missing properties yield a handle to
    /// `undefined` (queryable via type_of), not an error.
    fn get(&self, key: &str) -> PyResult<QjsHandle> {
        let context = self.context_ref()?.clone();
        let context_ptr = self.context_ptr;
        let persistent = self.persistent_clone()?;
        let new_pers = with_active_ctx(&context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let obj: Object<'_> = val.try_into_object().map_err(|v| {
                MarshalError::new_err(format!(
                    "handle target is not an object ({}), cannot get property",
                    v.type_of()
                ))
            })?;
            let result: Value<'_> = obj.get(key).map_err(|e| {
                QuickJSError::new_err(format!("get property {:?} failed: {}", key, e))
            })?;
            Ok(Persistent::save(ctx, result))
        })?;
        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(new_pers),
        })
    }

    /// Read a property by numeric index (array-like access). §6.4
    /// get_prop_index.
    fn get_index(&self, index: u32) -> PyResult<QjsHandle> {
        let context = self.context_ref()?.clone();
        let context_ptr = self.context_ptr;
        let persistent = self.persistent_clone()?;
        let new_pers = with_active_ctx(&context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let obj: Object<'_> = val.try_into_object().map_err(|v| {
                MarshalError::new_err(format!(
                    "handle target is not an object ({}), cannot get index",
                    v.type_of()
                ))
            })?;
            let result: Value<'_> = obj.get(index).map_err(|e| {
                QuickJSError::new_err(format!("get index {} failed: {}", index, e))
            })?;
            Ok(Persistent::save(ctx, result))
        })?;
        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(new_pers),
        })
    }

    /// Set a property. `value` may be a Python value (marshaled via
    /// py_to_js_value) or another QjsHandle — in which case we
    /// enforce the cross-context invariant first.
    fn set(&self, key: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let context = self.context_ref()?;
        let persistent = self.persistent_clone()?;
        let our_ctx_ptr = self.context_ptr;
        with_active_ctx(context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let obj: Object<'_> = val.try_into_object().map_err(|v| {
                MarshalError::new_err(format!(
                    "handle target is not an object ({}), cannot set property",
                    v.type_of()
                ))
            })?;
            let js_value = handle_or_py_to_js(ctx, value, our_ctx_ptr, 0)?;
            obj.set(key, js_value).map_err(|e| {
                QuickJSError::new_err(format!("set property {:?} failed: {}", key, e))
            })?;
            Ok(())
        })
    }

    /// True iff the object has property `key` whose value is not
    /// `undefined`. Collapses JS's "own property = undefined" /
    /// "not defined" distinction to "not present" (§7.3).
    fn has(&self, key: &str) -> PyResult<bool> {
        let context = self.context_ref()?;
        let persistent = self.persistent_clone()?;
        with_active_ctx(context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let obj: Object<'_> = val.try_into_object().map_err(|v| {
                MarshalError::new_err(format!(
                    "handle target is not an object ({}), cannot check property",
                    v.type_of()
                ))
            })?;
            if !obj.contains_key(key).map_err(|e| {
                QuickJSError::new_err(format!("contains_key {:?} failed: {}", key, e))
            })? {
                return Ok(false);
            }
            let v: Value<'_> = obj.get(key).map_err(|e| {
                QuickJSError::new_err(format!("get property {:?} failed: {}", key, e))
            })?;
            Ok(!matches!(
                v.type_of(),
                Type::Undefined | Type::Uninitialized
            ))
        })
    }

    /// Call this handle as a function. Each arg may be a Python
    /// value or a QjsHandle (cross-context-guarded).
    #[pyo3(signature = (*args))]
    fn call(&self, args: &Bound<'_, PyTuple>) -> PyResult<QjsHandle> {
        let context = self.context_ref()?.clone();
        let context_ptr = self.context_ptr;
        let persistent = self.persistent_clone()?;
        let new_pers = with_active_ctx(&context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let func: Function<'_> = val.try_into_function().map_err(|v| {
                MarshalError::new_err(format!(
                    "handle target is not callable ({})",
                    v.type_of()
                ))
            })?;
            let js_args = collect_js_args(ctx, args, context_ptr)?;
            let result: Result<Value<'_>, CaughtError<'_>> =
                func.call_arg(js_args).catch(ctx);
            match result {
                Ok(v) => Ok(Persistent::save(ctx, v)),
                Err(caught) => Err(js_error_from_caught(ctx, caught)),
            }
        })?;
        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(new_pers),
        })
    }

    /// Look up `name` on this object and call it with `args`.
    /// Convenience for `obj.get(name).call(...)` without the middle
    /// handle materializing.
    #[pyo3(signature = (name, *args))]
    fn call_method(
        &self,
        name: &str,
        args: &Bound<'_, PyTuple>,
    ) -> PyResult<QjsHandle> {
        let context = self.context_ref()?.clone();
        let context_ptr = self.context_ptr;
        let persistent = self.persistent_clone()?;
        let new_pers = with_active_ctx(&context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let obj: Object<'_> = val.try_into_object().map_err(|v| {
                MarshalError::new_err(format!(
                    "handle target is not an object ({})",
                    v.type_of()
                ))
            })?;
            let func: Function<'_> = obj.get(name).map_err(|e| {
                QuickJSError::new_err(format!("method lookup {:?} failed: {}", name, e))
            })?;
            let mut js_args = rquickjs::function::Args::new(ctx.clone(), args.len());
            js_args
                .this(obj.into_value())
                .map_err(|e| QuickJSError::new_err(format!("set this failed: {}", e)))?;
            for (i, arg) in args.iter().enumerate() {
                let v = handle_or_py_to_js(ctx, &arg, context_ptr, 0)?;
                js_args.push_arg(v).map_err(|e| {
                    QuickJSError::new_err(format!("arg {} push failed: {}", i, e))
                })?;
            }
            let result: Result<Value<'_>, CaughtError<'_>> =
                func.call_arg(js_args).catch(ctx);
            match result {
                Ok(v) => Ok(Persistent::save(ctx, v)),
                Err(caught) => Err(js_error_from_caught(ctx, caught)),
            }
        })?;
        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(new_pers),
        })
    }

    /// Call as a JS constructor (`new fn(args...)`). §6.4
    /// new_instance.
    #[pyo3(signature = (*args))]
    fn new_instance(&self, args: &Bound<'_, PyTuple>) -> PyResult<QjsHandle> {
        let context = self.context_ref()?.clone();
        let context_ptr = self.context_ptr;
        let persistent = self.persistent_clone()?;
        let new_pers = with_active_ctx(&context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let ctor: Constructor<'_> = val.try_into_constructor().map_err(|v| {
                MarshalError::new_err(format!(
                    "handle target is not a constructor ({})",
                    v.type_of()
                ))
            })?;
            let js_args = collect_js_args(ctx, args, context_ptr)?;
            let result: Result<Value<'_>, CaughtError<'_>> =
                ctor.construct_args(js_args).catch(ctx);
            match result {
                Ok(v) => Ok(Persistent::save(ctx, v)),
                Err(caught) => Err(js_error_from_caught(ctx, caught)),
            }
        })?;
        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(new_pers),
        })
    }

    /// Marshal to a Python value. With `allow_opaque=True`, values
    /// that would otherwise fail marshaling (functions, symbols,
    /// promises, proxies) are returned as child QjsHandle objects
    /// embedded in the result. §7.2.
    #[pyo3(signature = (*, allow_opaque=false))]
    fn to_python(&self, py: Python<'_>, allow_opaque: bool) -> PyResult<Py<PyAny>> {
        let context = self.context_ref()?.clone();
        let context_ptr = self.context_ptr;
        let persistent = self.persistent_clone()?;
        with_active_ctx(&context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            if allow_opaque {
                js_to_py_with_opaque(py, &val, &context, context_ptr, 0)
            } else {
                js_value_to_py(py, &val, 0)
            }
        })
    }

    /// Create a second handle to the same JS value. Both handles
    /// must be disposed independently. §6.4 dup.
    fn dup(&self) -> PyResult<QjsHandle> {
        let context = self.context_ref()?.clone();
        let context_ptr = self.context_ptr;
        let persistent = self.persistent_clone()?;
        // Re-save inside a with — each Persistent::save bumps the
        // underlying JSValue refcount via the cloned Value, so
        // independent dispose of both handles is correct.
        let new_pers = with_active_ctx(&context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            Ok(Persistent::save(ctx, val))
        })?;
        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(new_pers),
        })
    }

    /// Restore-and-drop the persistent ref inside a Ctx so QuickJS
    /// can decrement the JSValue refcount, then release the context
    /// clone. §6.7: rquickjs's Persistent has no Drop — Value::Drop
    /// needs a live Ctx to call JS_FreeValue. Forgetting this leaks
    /// the JS ref and trips list_empty(&rt->gc_obj_list) at runtime
    /// teardown. Idempotent.
    fn dispose(&mut self) -> PyResult<()> {
        if let (Some(context), Some(persistent)) = (self.context.take(), self.persistent.take()) {
            let _ = with_active_ctx(&context, |ctx| {
                let _ = persistent.restore(ctx);
                Ok(())
            });
        }
        Ok(())
    }

    fn is_disposed(&self) -> bool {
        self.persistent.is_none()
    }
}

// Fallback Drop: if Python's GC collects the handle without an
// explicit dispose(), release the JS ref here. The Python-side
// Handle.__del__ emits ResourceWarning (§7.3), but only if the owning
// Context is still alive; the drop here is defensive against both
// ordinary GC and the context-already-closed edge case.
impl Drop for QjsHandle {
    fn drop(&mut self) {
        if let (Some(context), Some(persistent)) = (self.context.take(), self.persistent.take()) {
            let _ = with_active_ctx(&context, |ctx| {
                let _ = persistent.restore(ctx);
                Ok(())
            });
        }
    }
}

impl QjsHandle {
    fn context_ref(&self) -> PyResult<&Context> {
        self.context
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("handle is disposed"))
    }

    fn persistent_clone(&self) -> PyResult<Persistent<Value<'static>>> {
        self.persistent
            .as_ref()
            .cloned()
            .ok_or_else(|| QuickJSError::new_err("handle is disposed"))
    }

    /// Run `f` against the live Value this handle wraps. Clones the
    /// persistent so the caller can keep using the handle.
    fn with_value<F, R>(&self, f: F) -> PyResult<R>
    where
        F: for<'js> FnOnce(&Ctx<'js>, &Value<'js>) -> PyResult<R>,
    {
        let context = self.context_ref()?;
        let persistent = self.persistent_clone()?;
        with_active_ctx(context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            f(ctx, &val)
        })
    }
}

/// Map an rquickjs Type to the string the Python API exposes via
/// Handle.type_of. Matches the v0.2 test cases exactly — "boolean"
/// not "bool", "number" for both Int/Float, "bigint" for big_int,
/// "object" for plain (Array/Function/Promise etc get their own
/// strings where meaningful).
fn type_name_of(t: Type) -> String {
    match t {
        Type::Null => "null",
        Type::Undefined | Type::Uninitialized => "undefined",
        Type::Bool => "boolean",
        Type::Int | Type::Float => "number",
        Type::BigInt => "bigint",
        Type::String => "string",
        Type::Symbol => "symbol",
        Type::Array => "array",
        Type::Function | Type::Constructor => "function",
        // Plain object, or JS types that `typeof` reports as "object"
        // (Promise, Exception, Proxy, Module). Tests key off these
        // strings so matching JS's own `typeof` keeps surprise low.
        Type::Object | Type::Promise | Type::Exception | Type::Proxy | Type::Module => "object",
        Type::Unknown => "unknown",
    }
    .to_string()
}

/// Marshal a Python value or QjsHandle into a JS value for the
/// current context. Cross-context handles raise InvalidHandleError.
fn handle_or_py_to_js<'js>(
    ctx: &Ctx<'js>,
    py_val: &Bound<'_, PyAny>,
    expected_ctx_ptr: usize,
    depth: u32,
) -> PyResult<Value<'js>> {
    if let Ok(handle) = py_val.cast::<QjsHandle>() {
        let borrow = handle.borrow();
        if borrow.context_ptr != expected_ctx_ptr {
            return Err(InvalidHandleError::new_err(
                "handle belongs to a different context",
            ));
        }
        let persistent = borrow.persistent_clone()?;
        return persistent.restore(ctx).map_err(map_handle_error);
    }
    py_to_js_value(ctx, py_val, depth)
}

/// Collect a Python tuple of args into an rquickjs `Args`, doing the
/// cross-context check for any embedded QjsHandle along the way.
fn collect_js_args<'js>(
    ctx: &Ctx<'js>,
    args: &Bound<'_, PyTuple>,
    expected_ctx_ptr: usize,
) -> PyResult<rquickjs::function::Args<'js>> {
    let mut js_args = rquickjs::function::Args::new(ctx.clone(), args.len());
    for (i, arg) in args.iter().enumerate() {
        let v = handle_or_py_to_js(ctx, &arg, expected_ctx_ptr, 0)?;
        js_args.push_arg(v).map_err(|e| {
            QuickJSError::new_err(format!("arg {} push failed: {}", i, e))
        })?;
    }
    Ok(js_args)
}

/// Like `js_value_to_py` but substitutes fresh QjsHandles at
/// positions that would otherwise raise MarshalError (functions,
/// symbols, promises). Recursively walks objects/arrays — mixed
/// marshalable / opaque contents work. §7.2 allow_opaque.
fn js_to_py_with_opaque<'js>(
    py: Python<'_>,
    val: &Value<'js>,
    context: &Context,
    context_ptr: usize,
    depth: u32,
) -> PyResult<Py<PyAny>> {
    if depth >= MAX_MARSHAL_DEPTH {
        return Err(depth_error());
    }
    match val.type_of() {
        // Opaque types get wrapped as child handles.
        Type::Function | Type::Constructor | Type::Symbol | Type::Promise | Type::Proxy => {
            let persistent = Persistent::save(val.ctx(), val.clone());
            let handle = QjsHandle {
                context: Some(context.clone()),
                context_ptr,
                persistent: Some(persistent),
            };
            Ok(handle.into_pyobject(py)?.into_any().unbind())
        }
        // Containers walk with the opaque-aware recursor.
        Type::Array => {
            let arr = val.as_array().expect("Type::Array has as_array");
            let list = PyList::empty(py);
            for item in arr.iter::<Value<'js>>() {
                let item = item.map_err(|e| {
                    MarshalError::new_err(format!("array element read failed: {}", e))
                })?;
                list.append(js_to_py_with_opaque(
                    py,
                    &item,
                    context,
                    context_ptr,
                    depth + 1,
                )?)?;
            }
            Ok(list.unbind().into_any())
        }
        Type::Object => {
            let obj = val.as_object().expect("Type::Object has as_object");
            // Uint8Array still short-circuits to bytes.
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
            for entry in obj.own_props::<String, Value<'js>>(Filter::default()) {
                let (key, value) = entry.map_err(|e| {
                    MarshalError::new_err(format!("object entry read failed: {}", e))
                })?;
                dict.set_item(
                    key,
                    js_to_py_with_opaque(py, &value, context, context_ptr, depth + 1)?,
                )?;
            }
            Ok(dict.unbind().into_any())
        }
        // Everything else delegates to the plain marshaler.
        _ => js_value_to_py(py, val, depth),
    }
}

/// Build and install a host function trampoline. Lives as a free
/// function so Rust can infer the closure lifetime as a single `'js`
/// threading through the Ctx arg, Rest<Value> args, and Value
/// return — a bare closure in the register_host_function body
/// splits those into three independent inferred lifetimes that
/// don't unify against the `Fn(Ctx<'js>, Rest<Value<'js>>) -> Value<'js>`
/// bound rquickjs needs.
fn build_host_trampoline<'js>(
    ctx: &Ctx<'js>,
    dispatcher: Py<PyAny>,
    fn_id: u32,
    name: &str,
) -> PyResult<Function<'js>> {
    let trampoline = move |cx: Ctx<'js>, args: rquickjs::function::Rest<Value<'js>>| -> rquickjs::Result<Value<'js>> {
        call_host_fn(&cx, &dispatcher, fn_id, args.0)
    };
    Function::new(ctx.clone(), trampoline)
        .map_err(|e| QuickJSError::new_err(format!("Function::new failed: {}", e)))?
        .with_name(name)
        .map_err(|e| QuickJSError::new_err(format!("Function::with_name failed: {}", e)))
}

/// Trampoline invoked by rquickjs when JS calls a registered host
/// function. Marshals args to Python, calls the dispatcher, marshals
/// the return value back to JS. On Python exception from the
/// dispatcher, constructs a JS Error whose name/message come from
/// the PyErr's type and args tuple (the dispatcher re-raises host-fn
/// exceptions as _engine.JSError(name="HostError", message, stack)),
/// throws it, and returns Error::Exception to unwind into JS.
fn call_host_fn<'js>(
    ctx: &Ctx<'js>,
    dispatcher: &Py<PyAny>,
    fn_id: u32,
    args: Vec<Value<'js>>,
) -> rquickjs::Result<Value<'js>> {
    // GIL is already held — we're inside ctx.eval which was called
    // from Python. Step 5 never releases the GIL during eval; step 9
    // may reconsider for async. `attach` is cheap when already held.
    Python::attach(|py| {
        // Marshal args JS → Python.
        let py_args: Vec<Py<PyAny>> = match args
            .iter()
            .map(|v| js_value_to_py(py, v, 0))
            .collect::<PyResult<Vec<_>>>()
        {
            Ok(v) => v,
            Err(e) => return Err(throw_host_error(ctx, &e, py)),
        };
        let args_tuple = pyo3::types::PyTuple::new(py, &py_args)
            .map_err(|e| throw_host_error(ctx, &e, py))?;

        // Call the Python dispatcher.
        let dispatcher = dispatcher.bind(py);
        let result = match dispatcher.call1((fn_id, args_tuple)) {
            Ok(r) => r,
            Err(e) => return Err(throw_host_error(ctx, &e, py)),
        };

        // Marshal return Python → JS.
        match py_to_js_value(ctx, &result, 0) {
            Ok(v) => Ok(v),
            Err(e) => Err(throw_host_error(ctx, &e, py)),
        }
    })
}

/// Convert a PyErr into a JS exception throw + `Error::Exception`.
/// If the PyErr is a `_engine.JSError` (which the dispatcher raises
/// on host-fn exceptions, with HostError's name/message/stack in
/// args), we thread those values into the JS Error. Otherwise it's
/// an infra-level Python exception — construct a generic Error with
/// the PyErr's string representation.
fn throw_host_error(ctx: &Ctx<'_>, err: &PyErr, py: Python<'_>) -> rquickjs::Error {
    // Extract (name, message, stack) if this is a JSError. On any
    // extraction failure, fall back to generic Error(str(err)).
    let (name, message) = extract_jserror_fields(err, py)
        .unwrap_or_else(|| ("Error".to_string(), err.to_string()));

    // Build the JS Error object: use Exception::from_message to get
    // a proper Error with a stack, then set .name to what the
    // dispatcher asked for.
    let exc = match rquickjs::Exception::from_message(ctx.clone(), &message) {
        Ok(e) => e,
        Err(_) => return rquickjs::Error::Exception,
    };
    let _ = exc.as_object().set(PredefinedAtom::Name, name);
    ctx.throw(exc.into_value())
}

fn extract_jserror_fields(err: &PyErr, py: Python<'_>) -> Option<(String, String)> {
    let value = err.value(py);
    // The JSError class was constructed with (name, message, stack).
    // Python's JSError.__init__ stores them as attributes, but the
    // Rust side's _engine.JSError is create_exception! — args live on
    // .args. The Python-side dispatcher re-raises _engine.JSError
    // with the (name, message, stack) tuple.
    let args = value.getattr("args").ok()?;
    let tup: &Bound<'_, pyo3::types::PyTuple> = args.cast().ok()?;
    if tup.len() < 2 {
        return None;
    }
    let name: String = tup.get_item(0).ok()?.extract().ok()?;
    let message: String = tup.get_item(1).ok()?.extract().ok()?;
    Some((name, message))
}

fn map_handle_error(err: Error) -> PyErr {
    // Error::UnrelatedRuntime is the canonical cross-context signal
    // from Persistent::restore — Python-side maps it to
    // InvalidHandleError in step 7. For step 4 a QuickJSError is
    // fine; globals don't cross contexts.
    QuickJSError::new_err(format!("handle restore failed: {}", err))
}

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
