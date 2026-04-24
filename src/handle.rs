//! QjsHandle — opaque reference to a JS value that outlives a
//! single eval. Wraps `Persistent<Value<'static>>` + a Context clone
//! for restore + the raw context pointer for cross-context identity
//! checks.

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyTuple};
use rquickjs::{
    function::Constructor, CatchResultExt, CaughtError, Context, Ctx, Function, Object, Persistent,
    Type, Value,
};

use crate::errors::{js_error_from_caught, map_handle_error, MarshalError, QuickJSError};
use crate::marshal::{
    collect_js_args, handle_or_py_to_js, js_to_py_with_opaque, js_value_to_py, type_name_of,
};
use crate::reentrance::with_active_ctx;

/// Holds a `Persistent<Value>` and the pieces needed to restore it:
/// a Context clone, plus the raw context pointer for cross-context
/// identity checks. / .
#[pyclass(module = "quickjs_rs._engine", unsendable)]
pub(crate) struct QjsHandle {
    pub(crate) context: Option<Context>,
    /// Raw JSContext pointer for cross-context identity checks.
    /// Populated at construction and never rewritten — stable for
    /// the handle's lifetime.
    pub(crate) context_ptr: usize,
    pub(crate) persistent: Option<Persistent<Value<'static>>>,
}

#[pymethods]
impl QjsHandle {
    /// Raw pointer of the context that created this handle. The
    /// Python Handle uses this to enforce the cross-context guard
    /// ("Handles are bound to their creating context").
    #[getter]
    fn context_id(&self) -> usize {
        self.context_ptr
    }

    /// Structural type tag — "object", "array", "function", "null",
    /// "undefined", "boolean", "number", "bigint", "string",
    /// "symbol". Maps rquickjs's internal Type enum to the strings
    /// the Python API (Handle.type_of) exposes.
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

    /// Read a property by numeric index (array-like access).     /// get_prop_index.
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
            let result: Value<'_> = obj
                .get(index)
                .map_err(|e| QuickJSError::new_err(format!("get index {} failed: {}", index, e)))?;
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
    /// "not defined" distinction to "not present" ().
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
                MarshalError::new_err(format!("handle target is not callable ({})", v.type_of()))
            })?;
            let js_args = collect_js_args(ctx, args, context_ptr)?;
            let result: Result<Value<'_>, CaughtError<'_>> = func.call_arg(js_args).catch(ctx);
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
    fn call_method(&self, name: &str, args: &Bound<'_, PyTuple>) -> PyResult<QjsHandle> {
        let context = self.context_ref()?.clone();
        let context_ptr = self.context_ptr;
        let persistent = self.persistent_clone()?;
        let new_pers = with_active_ctx(&context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let obj: Object<'_> = val.try_into_object().map_err(|v| {
                MarshalError::new_err(format!("handle target is not an object ({})", v.type_of()))
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
                js_args
                    .push_arg(v)
                    .map_err(|e| QuickJSError::new_err(format!("arg {} push failed: {}", i, e)))?;
            }
            let result: Result<Value<'_>, CaughtError<'_>> = func.call_arg(js_args).catch(ctx);
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

    /// Call as a JS constructor (`new fn(args...)`)
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
    /// embedded in the result.
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
    /// must be disposed independently.
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
    /// clone. rquickjs's Persistent has no Drop — Value::Drop
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
// Handle.__del__ emits ResourceWarning, but only if the
// owning Context is still alive; the drop here is defensive against
// both ordinary GC and the context-already-closed edge case.
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
    pub(crate) fn context_ref(&self) -> PyResult<&Context> {
        self.context
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("handle is disposed"))
    }

    pub(crate) fn persistent_clone(&self) -> PyResult<Persistent<Value<'static>>> {
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
