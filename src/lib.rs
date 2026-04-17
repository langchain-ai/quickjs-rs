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
    Array, BigInt, CatchResultExt, CaughtError, Context, Ctx, Error, Function, Object, Persistent,
    Runtime, String as JsString, Type, TypedArray, Value,
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
    /// Currently-active Ctx during a `Context::with(|ctx| ...)` scope.
    /// Used to handle reentrant eval from inside host functions: the
    /// outer `with` holds the runtime lock (a non-reentrant RefCell
    /// in rquickjs without the `parallel` feature), so an inner
    /// `Context::with` panics. Instead, reentrant evals run against
    /// this already-live Ctx directly.
    ///
    /// The 'static lifetime is a lie maintained by the bracketing in
    /// `with_active_ctx` — the slot is set on entry to a with-scope
    /// and cleared before leaving, so no `Ctx` handed out from it
    /// ever outlives its real `'js` scope. Same pattern as
    /// rquickjs::Persistent.
    active_ctx: std::cell::Cell<Option<Ctx<'static>>>,
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
            active_ctx: std::cell::Cell::new(None),
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
        self.with_active_ctx(|ctx| {
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
        self.with_active_ctx(|ctx| {
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
        let persistent = self.with_active_ctx(|ctx| {
            let globals = ctx.globals();
            Ok(Persistent::save(ctx, globals.into_value()))
        })?;
        Ok(QjsHandle {
            context: Some(ctx_owner),
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

    /// Run `f` with a live `Ctx<'js>`. If there's already an active
    /// Ctx (reentrant call from a host function), use it directly
    /// and skip re-entering `Context::with` — which would try to
    /// re-lock the runtime's non-reentrant RefCell and panic.
    /// Otherwise enter `Context::with` as usual and publish the Ctx
    /// into the slot for any nested call to find.
    fn with_active_ctx<F, R>(&self, f: F) -> PyResult<R>
    where
        F: FnOnce(&Ctx<'_>) -> PyResult<R>,
    {
        // Fast path: reentrant call. The stored Ctx<'static> was
        // placed by an outer with_active_ctx and is guaranteed live
        // until that call returns — we're executing inside its body
        // right now, so the lifetime shortening from 'static to a
        // shorter borrow here is sound.
        if let Some(stashed) = self.active_ctx.take() {
            let result = {
                let as_short: &Ctx<'_> = unsafe {
                    // Shrink 'static down to the local borrow. Both
                    // layouts are identical — Ctx has a NonNull +
                    // PhantomData marker, no actual lifetime data.
                    core::mem::transmute::<&Ctx<'static>, &Ctx<'_>>(&stashed)
                };
                f(as_short)
            };
            // Put the Ctx back so the outer scope's cleanup continues
            // working. We used .take() rather than .get() because
            // Ctx isn't Copy.
            self.active_ctx.set(Some(stashed));
            return result;
        }

        // Slow path: fresh Context::with, publish the Ctx into the
        // slot for the duration of the closure, clear on exit
        // (including panic unwind via the guard).
        let ctx_owner = self.context()?;
        let slot = &self.active_ctx;
        ctx_owner.with(|ctx| {
            let static_ctx: Ctx<'static> = unsafe {
                // Launder the 'js lifetime to 'static. Sound because
                // we clear the slot on every exit path below before
                // the real 'js scope ends. Same design as
                // rquickjs::Persistent, just scoped to a single call.
                core::mem::transmute::<Ctx<'_>, Ctx<'static>>(ctx.clone())
            };
            slot.set(Some(static_ctx));
            struct Guard<'a>(&'a std::cell::Cell<Option<Ctx<'static>>>);
            impl Drop for Guard<'_> {
                fn drop(&mut self) {
                    self.0.set(None);
                }
            }
            let _guard = Guard(slot);
            f(&ctx)
        })
    }
}

/// Minimal step-4 QjsHandle — owns a `Persistent<Value>` plus the
/// context reference needed to restore it. Step 7 adds the full
/// lifecycle (dispose, is_disposed, ResourceWarning, cross-context
/// guard, call/new_instance/to_python/type_of, etc). For now the
/// handle just needs to support globals-proxy prop access.
#[pyclass(module = "quickjs_rs._engine", unsendable)]
struct QjsHandle {
    context: Option<Context>,
    persistent: Option<Persistent<Value<'static>>>,
}

#[pymethods]
impl QjsHandle {
    /// Read a property from the object this handle wraps.
    /// Missing properties marshal as `undefined` → Python `None` at
    /// the root. Returns `MarshalError` only if the value itself
    /// isn't marshalable (function, symbol, etc).
    fn get_prop(&self, py: Python<'_>, key: &str) -> PyResult<Py<PyAny>> {
        let (context, persistent) = self.parts()?;
        let persistent = persistent.clone();
        context.with(|ctx| {
            let val = persistent.restore(&ctx).map_err(map_handle_error)?;
            let obj: Object<'_> = val.try_into_object().map_err(|v| {
                MarshalError::new_err(format!(
                    "handle target is not an object ({}), cannot get property",
                    v.type_of()
                ))
            })?;
            let result: Value<'_> = obj.get(key).map_err(|e| {
                QuickJSError::new_err(format!("get property {:?} failed: {}", key, e))
            })?;
            js_value_to_py(py, &result, 0)
        })
    }

    /// Set a property on the object this handle wraps.
    fn set_prop(&self, key: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let (context, persistent) = self.parts()?;
        let persistent = persistent.clone();
        context.with(|ctx| {
            let val = persistent.restore(&ctx).map_err(map_handle_error)?;
            let obj: Object<'_> = val.try_into_object().map_err(|v| {
                MarshalError::new_err(format!(
                    "handle target is not an object ({}), cannot set property",
                    v.type_of()
                ))
            })?;
            let js_value = py_to_js_value(&ctx, value, 0)?;
            obj.set(key, js_value).map_err(|e| {
                QuickJSError::new_err(format!("set property {:?} failed: {}", key, e))
            })?;
            Ok(())
        })
    }

    /// True iff the object has own-or-inherited property `key` whose
    /// value is not `undefined`. §7.3-adjacent: `in` collapses the
    /// "own but set to undefined" / "not defined" distinction that
    /// JS makes — both are "not present" Python-side. Matches the
    /// v0.2 tripwire test_contains_false_when_value_is_undefined.
    fn has_prop(&self, key: &str) -> PyResult<bool> {
        let (context, persistent) = self.parts()?;
        let persistent = persistent.clone();
        context.with(|ctx| {
            let val = persistent.restore(&ctx).map_err(map_handle_error)?;
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
            // Present: collapse `undefined` to "not present".
            let v: Value<'_> = obj.get(key).map_err(|e| {
                QuickJSError::new_err(format!("get property {:?} failed: {}", key, e))
            })?;
            Ok(!matches!(
                v.type_of(),
                Type::Undefined | Type::Uninitialized
            ))
        })
    }

    /// Restore-and-drop the persistent ref inside a Ctx so QuickJS
    /// can decrement the JSValue refcount, then release the context
    /// clone. Step 7 adds ResourceWarning / is_disposed / __del__.
    ///
    /// §6.7: rquickjs's Persistent has no Drop — it holds a Value
    /// with a 'static-lifetime lie, and Value::Drop needs a live
    /// Ctx to call JS_FreeValue. Forgetting to restore-drop leaks
    /// the JS ref and, on runtime teardown, triggers QuickJS's
    /// `list_empty(&rt->gc_obj_list)` assertion.
    fn dispose(&mut self) -> PyResult<()> {
        if let (Some(context), Some(persistent)) = (self.context.take(), self.persistent.take()) {
            context.with(|ctx| {
                // restore() produces a live Value whose Drop frees
                // the JS ref via the Ctx. The `let _ =` pattern
                // ensures the returned Value drops at the end of
                // this closure while ctx is still valid.
                let _ = persistent.restore(&ctx);
            });
        }
        Ok(())
    }
}

// Fallback Drop: if Python's GC disposes the handle without calling
// .dispose(), release the JS ref here. Step 7 adds the
// ResourceWarning that the v0.2 contract requires on leaked handles.
impl Drop for QjsHandle {
    fn drop(&mut self) {
        if let (Some(context), Some(persistent)) = (self.context.take(), self.persistent.take()) {
            context.with(|ctx| {
                let _ = persistent.restore(&ctx);
            });
        }
    }
}

impl QjsHandle {
    fn parts(&self) -> PyResult<(&Context, &Persistent<Value<'static>>)> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("handle is disposed"))?;
        let persistent = self
            .persistent
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("handle is disposed"))?;
        Ok((context, persistent))
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
    Ok(())
}
