//! QjsContext pyclass — wraps rquickjs::Context.
//!
//! Holds the sync + async host dispatchers, the pending-resolver
//! map for in-flight async host calls, and the flags that
//! propagate sync-eval-hit-async-call across the Python/Rust
//! boundary. All eval surfaces (sync, async, handle) route through
//! `with_active_ctx` so reentrant evals from within host functions
//! don't re-lock rquickjs's non-reentrant runtime RefCell.

use pyo3::prelude::*;
use pyo3::types::PyAny;
use pyo3::exceptions::PyValueError;
use rquickjs::qjs;
use rquickjs::{
    atom::PredefinedAtom, context::EvalOptions, CatchResultExt, CaughtError, Context, Module,
    Object, Persistent, Value,
};
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::mem::MaybeUninit;
use std::rc::Rc;

use crate::errors::{
    js_error_from_caught, map_handle_error, map_runtime_new_error, InvalidHandleError, QuickJSError,
};
use crate::handle::QjsHandle;
use crate::host_fn::{build_async_host_trampoline, build_host_trampoline, PendingResolver};
use crate::marshal::{js_value_to_py, py_to_js_value, type_name_of};
use crate::reentrance::with_active_ctx;
use crate::runtime::QjsRuntime;
use crate::snapshot::{
    SnapshotFlags, SnapshotManager, SnapshotNameRecord, SnapshotRecordKind, SnapshotState,
};

#[pyclass(module = "quickjs_rs._engine", unsendable)]
pub(crate) struct QjsContext {
    inner: Option<Context>,
    /// host function dispatch: a single Python-side callable
    /// that receives (fn_id, args_tuple) and returns the host fn's
    /// result. Set via set_host_call_dispatcher from the Python
    /// Context layer before any host function is registered.
    host_dispatcher: Option<Py<PyAny>>,
    /// async host function dispatcher. Called from the async
    /// trampoline with (fn_id, args_tuple, pending_id). Schedules
    /// an asyncio task; on completion it calls resolve_pending /
    /// reject_pending to settle the JS Promise.
    async_host_dispatcher: Option<Py<PyAny>>,
    /// pending Promise resolvers, keyed by host-allocated
    /// pending_id. Each entry is removed when resolve_pending or
    /// reject_pending settles it. On context close, remaining
    /// entries are drained + restored to release JSValue refs.
    /// Wrapped in `Rc` so the async trampoline closures
    /// can insert into it.
    pending_resolvers: Rc<RefCell<HashMap<u32, PendingResolver>>>,
    next_pending_id: Rc<Cell<u32>>,
    /// Set by the async trampoline when an async
    /// host fn is dispatched from inside a sync eval (i.e. while
    /// `in_sync_eval` is true). Context.eval consumes this after
    /// the eval returns and raises ConcurrentEvalError.
    sync_eval_hit_async_call: Rc<Cell<bool>>,
    /// set by Python Context.eval around the synchronous
    /// eval entry so the async trampoline can detect
    /// sync-eval-with-async-hostfn regardless of whether an
    /// asyncio loop is ambient.
    in_sync_eval: Rc<Cell<bool>>,
    /// Snapshot tracking + stateful serializer helpers.
    snapshot_state: SnapshotState,
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
            async_host_dispatcher: None,
            pending_resolvers: Rc::new(RefCell::new(HashMap::new())),
            next_pending_id: Rc::new(Cell::new(1)),
            sync_eval_hit_async_call: Rc::new(Cell::new(false)),
            in_sync_eval: Rc::new(Cell::new(false)),
            snapshot_state: SnapshotState::new(),
        })
    }

    /// Install the Python-side dispatcher. The Context layer calls
    /// this once at construction time. The dispatcher signature is
    /// `dispatcher(fn_id: int, args: tuple) -> Any` — on host-fn
    /// exception it re-raises `_engine.JSError(name, message,
    /// stack)` which the Rust trampoline catches and converts to a
    /// JS throw.
    fn set_host_call_dispatcher(&mut self, dispatcher: Py<PyAny>) -> PyResult<()> {
        self.host_dispatcher = Some(dispatcher);
        Ok(())
    }

    /// Install the async-host dispatcher. Invoked from the
    /// async trampoline when JS calls a registered async host fn.
    /// Signature: `dispatcher(fn_id: int, args: tuple, pending_id:
    /// int) -> int`. Returns 0 on successful scheduling, -1 if
    /// the call came from inside a sync eval (sets the
    /// sync_eval_hit_async_call flag — the Python sync-eval path
    /// consumes it and raises ConcurrentEvalError).
    fn set_async_host_dispatcher(&mut self, dispatcher: Py<PyAny>) -> PyResult<()> {
        self.async_host_dispatcher = Some(dispatcher);
        Ok(())
    }

    fn set_in_sync_eval(&self, value: bool) {
        self.in_sync_eval.set(value);
    }

    /// Pop the sync-eval-hit-async-call flag. Context.eval calls
    /// this after a sync eval to decide whether to raise
    /// ConcurrentEvalError.
    fn take_sync_eval_hit_async_call(&self) -> bool {
        self.sync_eval_hit_async_call.replace(false)
    }

    fn close(&mut self) -> PyResult<()> {
        // Free any outstanding pending resolvers before the
        // Context drops. Each Persistent<Function> holds a JSValue
        // ref that needs a live Ctx to release. Forgetting
        // this trips QuickJS's list_empty(&rt->gc_obj_list) at
        // runtime teardown.
        if let Some(context) = self.inner.as_ref() {
            let entries: Vec<(u32, PendingResolver)> =
                self.pending_resolvers.borrow_mut().drain().collect();
            if !entries.is_empty() {
                let _ = with_active_ctx(context, |ctx| {
                    for (_, entry) in entries {
                        let _ = entry.resolve.restore(ctx);
                        let _ = entry.reject.restore(ctx);
                    }
                    Ok(())
                });
            }
        }
        self.inner = None;
        Ok(())
    }

    fn is_closed(&self) -> bool {
        self.inner.is_none()
    }

    /// Test/debug helper exposing the tracked declaration registry.
    fn debug_snapshot_registry_names(&self) -> PyResult<Vec<String>> {
        let _ = self.context()?;
        Ok(self.snapshot_state.debug_registry_names())
    }

    #[pyo3(signature = (*, on_unserializable="tombstone", on_missing_name="skip", allow_bytecode=false, allow_reference=true, allow_sab=false))]
    fn create_snapshot(
        &self,
        on_unserializable: &str,
        on_missing_name: &str,
        allow_bytecode: bool,
        allow_reference: bool,
        allow_sab: bool,
    ) -> PyResult<Vec<u8>> {
        let _ = self.context()?;
        let flags = SnapshotFlags {
            allow_bytecode,
            allow_reference,
            allow_sab,
        };
        SnapshotManager::create_snapshot(
            self,
            &self.snapshot_state,
            on_unserializable,
            on_missing_name,
            flags,
        )
    }

    #[pyo3(signature = (handle, *, allow_bytecode=false, allow_reference=true, allow_sab=false))]
    fn dump_handle(
        &self,
        handle: &QjsHandle,
        allow_bytecode: bool,
        allow_reference: bool,
        allow_sab: bool,
    ) -> PyResult<Vec<u8>> {
        if handle.context_ptr != self.context()?.as_raw().as_ptr() as usize {
            return Err(InvalidHandleError::new_err(
                "handle belongs to a different context",
            ));
        }
        let persistent = handle.persistent_clone()?;
        let context = self.context()?;
        let ctx_ptr = context.as_raw().as_ptr();
        with_active_ctx(context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let mut out = MaybeUninit::<u64>::uninit();
            let flags = Self::write_object_flags(allow_bytecode, allow_reference, allow_sab);
            let buf = unsafe { qjs::JS_WriteObject(ctx_ptr, out.as_mut_ptr(), val.as_raw(), flags) };
            if buf.is_null() {
                return Err(Self::ctx_exception_pyerr(ctx));
            }

            let len = unsafe { out.assume_init() };
            let bytes = unsafe { std::slice::from_raw_parts(buf, len as usize) }.to_vec();
            unsafe { qjs::js_free(ctx_ptr, buf as *mut _) };
            Ok(bytes)
        })
    }

    #[pyo3(signature = (data, *, allow_bytecode=false, allow_reference=true, allow_sab=false))]
    fn load_handle(
        &self,
        data: &[u8],
        allow_bytecode: bool,
        allow_reference: bool,
        allow_sab: bool,
    ) -> PyResult<QjsHandle> {
        let context = self.context()?.clone();
        let context_ptr = context.as_raw().as_ptr() as usize;
        let ctx_ptr = context.as_raw().as_ptr();
        let persistent = with_active_ctx(&context, |ctx| {
            let flags = Self::read_object_flags(allow_bytecode, allow_reference, allow_sab);
            let raw_val =
                unsafe { qjs::JS_ReadObject(ctx_ptr, data.as_ptr(), data.len() as u64, flags) };
            if unsafe { qjs::JS_IsException(raw_val) } {
                return Err(Self::ctx_exception_pyerr(ctx));
            }
            let restored = unsafe { Value::from_raw(ctx.clone(), raw_val) };
            Ok(Persistent::save(ctx, restored))
        })?;

        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(persistent),
        })
    }

    #[pyo3(signature = (data, *, inject_globals=true))]
    fn restore_snapshot_bytes(&self, data: &[u8], inject_globals: bool) -> PyResult<()> {
        SnapshotManager::restore_snapshot_bytes(self, &self.snapshot_state, data, inject_globals)
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
        self.snapshot_state.track_eval(code, module);
        with_active_ctx(context, |ctx| {
            let mut options = EvalOptions::default();
            options.global = !module;
            options.strict = strict;
            options.filename = Some(filename.to_string());
            let result: Result<Value<'_>, CaughtError<'_>> = ctx
                .eval_with_options::<Value<'_>, _>(code, options)
                .catch(ctx);
            match result {
                Ok(val) => js_value_to_py(py, &val, 0),
                Err(caught) => Err(js_error_from_caught(ctx, caught)),
            }
        })
    }

    /// Eval that returns a QjsHandle instead of marshaling the
    /// result to Python. eval_handle.
    ///
    /// `promise=true` sets QuickJS's JS_EVAL_FLAG_ASYNC so the
    /// evaluator enables top-level `await` and returns a Promise
    /// that resolves to `{value, done}`. eval_async uses this;
    /// ordinary sync eval_handle doesn't.
    #[pyo3(signature = (code, *, module=false, strict=false, promise=false, filename="<eval>"))]
    fn eval_handle(
        &self,
        code: &str,
        module: bool,
        strict: bool,
        promise: bool,
        filename: &str,
    ) -> PyResult<QjsHandle> {
        self.snapshot_state.track_eval(code, module);
        let context = self.context()?.clone();
        let context_ptr = context.as_raw().as_ptr() as usize;
        let persistent = with_active_ctx(&context, |ctx| {
            let mut options = EvalOptions::default();
            options.global = !module;
            options.strict = strict;
            options.promise = promise;
            options.filename = Some(filename.to_string());
            let result: Result<Value<'_>, CaughtError<'_>> = ctx
                .eval_with_options::<Value<'_>, _>(code, options)
                .catch(ctx);
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

    /// ES-module eval. Parses `code` as an ES module (name =
    /// `filename`), registers it with QuickJS, and starts its
    /// evaluation. Returns a QjsHandle wrapping the Promise that
    /// resolves to `undefined` once the module (and its full
    /// import graph) has finished evaluating.
    ///
    /// Unlike script-mode eval with JS_EVAL_FLAG_ASYNC, this uses
    /// `Module::evaluate`, which is QuickJS's actual ES-module entry
    /// point — `import` / `export` work, module-scoped bindings don't
    /// leak to global, and the module cache is consulted for each
    /// imported specifier.
    ///
    /// The Python driving loop (Context._run_inside_task_group)
    /// treats the returned handle the same as any other pending
    /// Promise — `run_pending_jobs` advances module loads and host
    /// callbacks, `promise_state` polls for settlement, rejections
    /// surface through `promise_result` + _classify_jserror.
    #[pyo3(signature = (code, *, filename="<eval>"))]
    fn eval_module_async(&self, code: &str, filename: &str) -> PyResult<QjsHandle> {
        self.snapshot_state.track_eval(code, true);
        let context = self.context()?.clone();
        let context_ptr = context.as_raw().as_ptr() as usize;
        let persistent = with_active_ctx(&context, |ctx| {
            let promise = Module::evaluate(ctx.clone(), filename, code)
                .catch(ctx)
                .map_err(|caught| js_error_from_caught(ctx, caught))?;
            Ok(Persistent::save(ctx, promise.into_value()))
        })?;
        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(persistent),
        })
    }

    /// Host function registration. Installs a JS function on
    /// `globalThis` under `name` that, when called from JS,
    /// marshals args to Python, calls the dispatcher, and marshals
    /// the return value back to JS. On Python exception, throws a
    /// JS Error whose name/message come from the _engine.JSError
    /// re-raised by the Python dispatcher.
    ///
    /// With `is_async=true`, the trampoline creates a JS Promise,
    /// stashes (resolve, reject) keyed by a fresh pending_id, calls
    /// the Python async dispatcher with (fn_id, args, pending_id)
    /// — the dispatcher schedules an asyncio task that calls
    /// resolve_pending/reject_pending when done — and returns the
    /// Promise synchronously to JS.
    #[pyo3(signature = (name, fn_id, is_async=false))]
    fn register_host_function(
        &self,
        py: Python<'_>,
        name: &str,
        fn_id: u32,
        is_async: bool,
    ) -> PyResult<()> {
        let name_owned = name.to_string();
        let context = self.context()?;
        if is_async {
            let dispatcher = self
                .async_host_dispatcher
                .as_ref()
                .ok_or_else(|| QuickJSError::new_err("async_host_dispatcher not set"))?
                .clone_ref(py);
            let pending = Rc::clone(&self.pending_resolvers);
            let next_id = Rc::clone(&self.next_pending_id);
            let sync_hit = Rc::clone(&self.sync_eval_hit_async_call);
            let in_sync = Rc::clone(&self.in_sync_eval);
            with_active_ctx(context, |ctx| {
                let js_fn = build_async_host_trampoline(
                    ctx,
                    dispatcher,
                    fn_id,
                    &name_owned,
                    pending,
                    next_id,
                    sync_hit,
                    in_sync,
                )?;
                ctx.globals().set(name_owned.clone(), js_fn).map_err(|e| {
                    QuickJSError::new_err(format!(
                        "failed to install async host function {:?} on globalThis: {}",
                        name_owned, e
                    ))
                })?;
                Ok(())
            })
        } else {
            let dispatcher = self
                .host_dispatcher
                .as_ref()
                .ok_or_else(|| QuickJSError::new_err("host_dispatcher not set"))?
                .clone_ref(py);
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
    }

    /// Called by the Python driving loop to resolve a
    /// pending-host-call Promise with `value`. `value` is marshaled
    /// via py_to_js_value. Missing pending_id is a benign no-op
    /// (double-resolve/close-race treated as idempotent).
    fn resolve_pending(&self, pending_id: u32, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let context = self.context()?;
        let entry = match self.pending_resolvers.borrow_mut().remove(&pending_id) {
            Some(e) => e,
            None => return Ok(()),
        };
        with_active_ctx(context, |ctx| {
            let resolve = entry.resolve.restore(ctx).map_err(map_handle_error)?;
            let _ = entry.reject.restore(ctx); // drop to free the JS ref
            let js_value = py_to_js_value(ctx, value, 0)?;
            let _: Value<'_> = resolve
                .call((js_value,))
                .map_err(|e| QuickJSError::new_err(format!("resolve call failed: {}", e)))?;
            Ok(())
        })
    }

    /// Called by the Python driving loop to reject a
    /// pending-host-call Promise with a JS Error carrying
    /// (name, message, stack). Missing pending_id: benign no-op.
    #[pyo3(signature = (pending_id, name, message, stack=None))]
    fn reject_pending(
        &self,
        pending_id: u32,
        name: &str,
        message: &str,
        stack: Option<&str>,
    ) -> PyResult<()> {
        let context = self.context()?;
        let entry = match self.pending_resolvers.borrow_mut().remove(&pending_id) {
            Some(e) => e,
            None => return Ok(()),
        };
        with_active_ctx(context, |ctx| {
            let _ = entry.resolve.restore(ctx); // drop to free
            let reject = entry.reject.restore(ctx).map_err(map_handle_error)?;
            let exc = rquickjs::Exception::from_message(ctx.clone(), message).map_err(|e| {
                QuickJSError::new_err(format!("Exception::from_message failed: {}", e))
            })?;
            let _ = exc.as_object().set(PredefinedAtom::Name, name.to_string());
            if let Some(s) = stack {
                let _ = exc.as_object().set(PredefinedAtom::Stack, s.to_string());
            }
            let _: Value<'_> = reject
                .call((exc.into_value(),))
                .map_err(|e| QuickJSError::new_err(format!("reject call failed: {}", e)))?;
            Ok(())
        })
    }

    /// Promise state — 0=pending, 1=fulfilled, 2=rejected,
    /// -1=not a promise. Used by the driving loop.
    fn promise_state(&self, handle: &QjsHandle) -> PyResult<i32> {
        if handle.context_ptr != self.context()?.as_raw().as_ptr() as usize {
            return Err(InvalidHandleError::new_err(
                "handle belongs to a different context",
            ));
        }
        let persistent = handle.persistent_clone()?;
        let context = self.context()?;
        with_active_ctx(context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            if !val.is_promise() {
                return Ok(-1);
            }
            let promise = val.as_promise().expect("is_promise");
            Ok(match promise.state() {
                rquickjs::promise::PromiseState::Pending => 0,
                rquickjs::promise::PromiseState::Resolved => 1,
                rquickjs::promise::PromiseState::Rejected => 2,
            })
        })
    }

    /// Return a QjsHandle to the Promise's settled value
    /// (result or reason). For Rejected, the reason comes via
    /// `ctx.catch()` — rquickjs's `Promise::result::<Value>()`
    /// returns `Some(Err(_))` on rejection and parks the actual
    /// reason on `Ctx::catch`.
    fn promise_result(&self, handle: &QjsHandle) -> PyResult<QjsHandle> {
        if handle.context_ptr != self.context()?.as_raw().as_ptr() as usize {
            return Err(InvalidHandleError::new_err(
                "handle belongs to a different context",
            ));
        }
        let persistent = handle.persistent_clone()?;
        let context = self.context()?.clone();
        let context_ptr = handle.context_ptr;
        let new_pers = with_active_ctx(&context, |ctx| {
            let val = persistent.restore(ctx).map_err(map_handle_error)?;
            let promise = val
                .as_promise()
                .ok_or_else(|| QuickJSError::new_err("promise_result on a non-promise handle"))?;
            let settled: Option<rquickjs::Result<Value<'_>>> = promise.result();
            let settled_val: Value<'_> = match settled {
                Some(Ok(v)) => v,
                Some(Err(_)) => ctx.catch(),
                None => Value::new_undefined(ctx.clone()),
            };
            Ok(Persistent::save(ctx, settled_val))
        })?;
        Ok(QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(new_pers),
        })
    }

    /// Drain QuickJS's job queue (Promise reactions).
    /// Returns negative on job-error, otherwise the count of jobs
    /// executed. The driving loop polls this between
    /// promise-state checks.
    fn run_pending_jobs(&self) -> PyResult<i32> {
        let context = self.context()?;
        let runtime = context.runtime().clone();
        let mut count: i32 = 0;
        loop {
            match runtime.execute_pending_job() {
                Ok(true) => count += 1,
                Ok(false) => break,
                Err(_) => {
                    return with_active_ctx(context, |ctx| {
                        let caught = ctx.catch();
                        Err(js_error_from_caught(
                            ctx,
                            rquickjs::CaughtError::Value(caught),
                        ))
                    });
                }
            }
        }
        Ok(count)
    }

    /// Return a handle to the global object. global_object.
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
    fn ctx_exception_pyerr<'js>(ctx: &rquickjs::Ctx<'js>) -> PyErr {
        let caught = ctx.catch();
        js_error_from_caught(ctx, rquickjs::CaughtError::Value(caught))
    }

    fn write_object_flags(allow_bytecode: bool, allow_reference: bool, allow_sab: bool) -> i32 {
        let mut flags = 0;
        if allow_bytecode {
            flags |= qjs::JS_WRITE_OBJ_BYTECODE;
        }
        if allow_reference {
            flags |= qjs::JS_WRITE_OBJ_REFERENCE;
        }
        if allow_sab {
            flags |= qjs::JS_WRITE_OBJ_SAB;
        }
        flags as i32
    }

    fn read_object_flags(allow_bytecode: bool, allow_reference: bool, allow_sab: bool) -> i32 {
        let mut flags = 0;
        if allow_bytecode {
            flags |= qjs::JS_READ_OBJ_BYTECODE;
        }
        if allow_reference {
            flags |= qjs::JS_READ_OBJ_REFERENCE;
        }
        if allow_sab {
            flags |= qjs::JS_READ_OBJ_SAB;
        }
        flags as i32
    }

    pub(crate) fn has_pending_snapshot_resolvers(&self) -> bool {
        !self.pending_resolvers.borrow().is_empty()
    }

    pub(crate) fn snapshot_resolve_name_handle(&self, name: &str) -> PyResult<QjsHandle> {
        self.eval_handle(name, false, false, false, "<snapshot:resolve-name>")
    }

    pub(crate) fn snapshot_handle_type(&self, handle: &QjsHandle) -> PyResult<String> {
        let persistent = handle.persistent_clone()?;
        let context = self.context()?;
        with_active_ctx(context, |ctx| {
            let value = persistent.restore(ctx).map_err(map_handle_error)?;
            Ok(type_name_of(value.type_of()))
        })
    }

    pub(crate) fn snapshot_dump_handle_bytes(
        &self,
        handle: &QjsHandle,
        flags: SnapshotFlags,
    ) -> PyResult<Vec<u8>> {
        self.dump_handle(
            handle,
            flags.allow_bytecode,
            flags.allow_reference,
            flags.allow_sab,
        )
    }

    pub(crate) fn snapshot_load_handle_bytes(
        &self,
        data: &[u8],
        flags: SnapshotFlags,
    ) -> PyResult<QjsHandle> {
        self.load_handle(
            data,
            flags.allow_bytecode,
            flags.allow_reference,
            flags.allow_sab,
        )
    }

    pub(crate) fn snapshot_dump_active_values_blob(
        &self,
        active_values: &[(String, Persistent<Value<'static>>)],
        flags: SnapshotFlags,
    ) -> PyResult<Vec<u8>> {
        let context = self.context()?.clone();
        let context_ptr = context.as_raw().as_ptr() as usize;
        let aggregate = with_active_ctx(&context, |ctx| {
            let obj = Object::new(ctx.clone())
                .map_err(|e| QuickJSError::new_err(format!("snapshot object alloc failed: {}", e)))?;
            for (name, persistent) in active_values {
                let value = persistent.clone().restore(ctx).map_err(map_handle_error)?;
                obj.set(name.as_str(), value).map_err(|e| {
                    QuickJSError::new_err(format!(
                        "snapshot aggregate set failed for {:?}: {}",
                        name, e
                    ))
                })?;
            }
            Ok(Persistent::save(ctx, obj.into_value()))
        })?;
        let aggregate_handle = QjsHandle {
            context: Some(context),
            context_ptr,
            persistent: Some(aggregate),
        };
        self.snapshot_dump_handle_bytes(&aggregate_handle, flags)
    }

    pub(crate) fn snapshot_inject_active_globals(
        &self,
        loaded: &QjsHandle,
        records: &[SnapshotNameRecord],
    ) -> PyResult<Vec<String>> {
        let context = self.context()?;
        let aggregate = loaded.persistent_clone()?;
        with_active_ctx(context, |ctx| {
            let value = aggregate.restore(ctx).map_err(map_handle_error)?;
            let obj: Object<'_> = value.try_into_object().map_err(|_| {
                PyValueError::new_err("snapshot values blob did not decode into an object")
            })?;
            let globals = ctx.globals();
            let mut names = Vec::new();
            for record in records {
                if record.record_kind != SnapshotRecordKind::Active {
                    continue;
                }
                if !obj.contains_key(record.name.as_str()).map_err(|e| {
                    QuickJSError::new_err(format!(
                        "snapshot aggregate lookup failed for {:?}: {}",
                        record.name, e
                    ))
                })? {
                    return Err(PyValueError::new_err(format!(
                        "snapshot is missing active key {:?} in values blob",
                        record.name
                    )));
                }
                let restored: Value<'_> = obj.get(record.name.as_str()).map_err(|e| {
                    QuickJSError::new_err(format!(
                        "snapshot aggregate get failed for {:?}: {}",
                        record.name, e
                    ))
                })?;
                globals.set(record.name.as_str(), restored).map_err(|e| {
                    QuickJSError::new_err(format!(
                        "failed to inject restored global {:?}: {}",
                        record.name, e
                    ))
                })?;
                names.push(record.name.clone());
            }
            Ok(names)
        })
    }

    pub(crate) fn snapshot_install_tombstones(
        &self,
        records: &[SnapshotNameRecord],
    ) -> PyResult<()> {
        let context = self.context()?;
        with_active_ctx(context, |ctx| {
            for record in records {
                if record.record_kind == SnapshotRecordKind::Active {
                    continue;
                }
                let hint = record
                    .hint
                    .clone()
                    .unwrap_or_else(|| "Value is unavailable after snapshot restore.".to_string());
                let name_json = serde_json::to_string(&record.name).map_err(|e| {
                    QuickJSError::new_err(format!(
                        "failed to encode tombstone name {:?}: {}",
                        record.name, e
                    ))
                })?;
                let hint_json = serde_json::to_string(&hint).map_err(|e| {
                    QuickJSError::new_err(format!(
                        "failed to encode tombstone hint for {:?}: {}",
                        record.name, e
                    ))
                })?;
                let code = format!(
                    "Object.defineProperty(globalThis, {name_json}, {{ \
configurable: true, enumerable: true, get() {{ throw new Error({hint_json}); }} \
}});"
                );
                let res: Result<(), CaughtError<'_>> = ctx.eval::<(), _>(code).catch(ctx);
                if let Err(caught) = res {
                    return Err(js_error_from_caught(ctx, caught));
                }
            }
            Ok(())
        })
    }

    pub(crate) fn context(&self) -> PyResult<&Context> {
        self.inner
            .as_ref()
            .ok_or_else(|| QuickJSError::new_err("context is closed"))
    }
}
