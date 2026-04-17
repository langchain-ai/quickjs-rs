//! §6.6 value marshaling — JS↔Python conversion.
//!
//! Split into:
//!   * `js_value_to_py` — the plain marshaler (MarshalError on
//!     functions/symbols/promises/etc).
//!   * `py_to_js_value` — the reverse direction.
//!   * `js_to_py_with_opaque` — walks containers like
//!     `js_value_to_py` but substitutes QjsHandle wrappers at
//!     would-be-error positions. Powers `Handle.to_python(
//!     allow_opaque=True)`.
//!   * `handle_or_py_to_js` / `collect_js_args` — helpers for the
//!     Handle surface that accept "Python value OR Handle" uniformly
//!     while enforcing the cross-context invariant.
//!   * `Undefined` — the sentinel class for nested JS `undefined`
//!     values that need to survive a round-trip (§6.6 preserve-
//!     undefined).

use pyo3::prelude::*;
use pyo3::types::{
    PyAny, PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple,
};
use rquickjs::{
    convert::{Coerced, FromJs},
    object::Filter,
    Array, BigInt, Context, Ctx, Function, Object, Persistent, String as JsString, Type,
    TypedArray, Value,
};

use crate::errors::{InvalidHandleError, MarshalError, QuickJSError};
use crate::handle::QjsHandle;

/// §8 invariant: depth cap for recursive marshaling. Matches v0.2's
/// bridge (cycle detection via depth limit, no ref tracking). Cycles
/// go around the guard only by being genuinely deeper than 128 — at
/// which point the MarshalError is the right signal regardless.
pub(crate) const MAX_MARSHAL_DEPTH: u32 = 128;

/// Sentinel for JS `undefined` when it appears nested inside a
/// structure. §6.6: distinct from `None` so a future
/// `preserve_undefined=True` mode can keep the distinction at the
/// root too. Equality is instance-agnostic: any two Undefined
/// objects compare equal, so Rust creating fresh instances is fine.
#[pyclass(
    module = "quickjs_rs._engine",
    frozen,
    eq,
    hash,
    str = "Undefined",
    skip_from_py_object
)]
#[derive(PartialEq, Eq, Hash, Clone)]
pub(crate) struct Undefined;

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

fn depth_error() -> PyErr {
    MarshalError::new_err(format!(
        "recursion limit of {} exceeded while marshaling (cycle or deeply nested structure)",
        MAX_MARSHAL_DEPTH
    ))
}

/// Marshal a JS value to a Python object per §6.6. `depth` is the
/// current recursion level; start at 0.
pub(crate) fn js_value_to_py<'js>(
    py: Python<'_>,
    val: &Value<'js>,
    depth: u32,
) -> PyResult<Py<PyAny>> {
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
pub(crate) fn py_to_js_value<'js>(
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

    // bool check before int — Python bool is a subclass of int. If
    // we checked int first, True would marshal as JS number 1
    // instead of JS true.
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
                // Larger than i64: use globalThis.BigInt(str).
                let s: String = i.str()?.to_str()?.to_string();
                let bigint_fn: Function<'js> = ctx
                    .globals()
                    .get("BigInt")
                    .map_err(|e| {
                        MarshalError::new_err(format!(
                            "BigInt constructor unavailable: {}",
                            e
                        ))
                    })?;
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
                        key.get_type()
                            .name()
                            .map(|s| s.to_string())
                            .unwrap_or_else(|_| "?".into())
                    ))
                })?
                .to_str()?;
            let v = py_to_js_value(ctx, &value, depth + 1)?;
            obj.set(key_str, v).map_err(|e| {
                QuickJSError::new_err(format!(
                    "Object set failed for key {:?}: {}",
                    key_str, e
                ))
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

/// Map an rquickjs Type to the string the Python API exposes via
/// Handle.type_of. Matches the v0.2 test cases exactly — "boolean"
/// not "bool", "number" for both Int/Float, "bigint" for big_int,
/// "object" for plain (Array/Function/Promise etc get their own
/// strings where meaningful).
pub(crate) fn type_name_of(t: Type) -> String {
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
        // Plain object, or JS types that `typeof` reports as
        // "object" (Promise, Exception, Proxy, Module). Tests key
        // off these strings so matching JS's own `typeof` keeps
        // surprise low.
        Type::Object | Type::Promise | Type::Exception | Type::Proxy | Type::Module => "object",
        Type::Unknown => "unknown",
    }
    .to_string()
}

/// Marshal a Python value or QjsHandle into a JS value for the
/// current context. Cross-context handles raise
/// `InvalidHandleError`.
pub(crate) fn handle_or_py_to_js<'js>(
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
        return persistent.restore(ctx).map_err(crate::errors::map_handle_error);
    }
    py_to_js_value(ctx, py_val, depth)
}

/// Collect a Python tuple of args into an rquickjs `Args`, doing
/// the cross-context check for any embedded QjsHandle along the
/// way.
pub(crate) fn collect_js_args<'js>(
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
pub(crate) fn js_to_py_with_opaque<'js>(
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
