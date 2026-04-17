//! §6.3 QjsContext pyclass — wraps rquickjs::Context.
//!
//! Holds the sync + async host dispatchers, the pending-resolver
//! map for in-flight async host calls (§7.4), and the flags that
//! propagate sync-eval-hit-async-call across the Python/Rust
//! boundary. All eval surfaces (sync, async, handle) route through
//! `with_active_ctx` so reentrant evals from within host functions
//! don't re-lock rquickjs's non-reentrant runtime RefCell (§6.7).

use pyo3::prelude::*;
use pyo3::types::PyAny;
use rquickjs::{
    atom::PredefinedAtom, context::EvalOptions, CatchResultExt, CaughtError, Context, Persistent,
    Value,
};
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;

use crate::errors::{
    js_error_from_caught, map_handle_error, map_runtime_new_error, InvalidHandleError,
    QuickJSError,
};
use crate::handle::QjsHandle;
use crate::host_fn::{build_async_host_trampoline, build_host_trampoline, PendingResolver};
use crate::marshal::{js_value_to_py, py_to_js_value};
use crate::reentrance::with_active_ctx;
use crate::runtime::QjsRuntime;

#[pyclass(module = "quickjs_rs._engine", unsendable)]
pub(crate) struct QjsContext {
    inner: Option<Context>,
    /// §6.5 host function dispatch: a single Python-side callable
    /// that receives (fn_id, args_tuple) and returns the host fn's
    /// result. Set via set_host_call_dispatcher from the Python
    /// Context layer before any host function is registered.
    host_dispatcher: Option<Py<PyAny>>,
    /// §7.4 async host function dispatcher. Called from the async
    /// trampoline with (fn_id, args_tuple, pending_id). Schedules
    /// an asyncio task; on completion it calls resolve_pending /
    /// reject_pending to settle the JS Promise.
    async_host_dispatcher: Option<Py<PyAny>>,
    /// §7.4 pending Promise resolvers, keyed by host-allocated
    /// pending_id. Each entry is removed when resolve_pending or
    /// reject_pending settles it. On context close, remaining
    /// entries are drained + restored to release JSValue refs
    /// (§6.7). Wrapped in `Rc` so the async trampoline closures
    /// can insert into it.
    pending_resolvers: Rc<RefCell<HashMap<u32, PendingResolver>>>,
    next_pending_id: Rc<Cell<u32>>,
    /// §7.4 / §10.3: set by the async trampoline when an async
    /// host fn is dispatched from inside a sync eval (i.e. while
    /// `in_sync_eval` is true). Context.eval consumes this after
    /// the eval returns and raises ConcurrentEvalError.
    sync_eval_hit_async_call: Rc<Cell<bool>>,
    /// §7.4: set by Python Context.eval around the synchronous
    /// eval entry so the async trampoline can detect
    /// sync-eval-with-async-hostfn regardless of whether an
    /// asyncio loop is ambient.
    in_sync_eval: Rc<Cell<bool>>,
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

    /// §7.4: install the async-host dispatcher. Invoked from the
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
        // §7.4: free any outstanding pending resolvers before the
        // Context drops. Each Persistent<Function> holds a JSValue
        // ref that needs a live Ctx to release (§6.7). Forgetting
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
    /// result to Python. §6.3 eval_handle.
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
        let context = self.context()?.clone();
        let context_ptr = context.as_raw().as_ptr() as usize;
        let persistent = with_active_ctx(&context, |ctx| {
            let mut options = EvalOptions::default();
            options.global = !module;
            options.strict = strict;
            options.promise = promise;
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

    /// §6.5 host function registration. Installs a JS function on
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
    /// Promise synchronously to JS. §7.4.
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
                    ctx, dispatcher, fn_id, &name_owned, pending, next_id, sync_hit, in_sync,
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

    /// §7.4: called by the Python driving loop to resolve a
    /// pending-host-call Promise with `value`. `value` is marshaled
    /// via py_to_js_value. Missing pending_id is a benign no-op
    /// (§6.4: double-resolve/close-race treated as idempotent).
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
            let _: Value<'_> = resolve.call((js_value,)).map_err(|e| {
                QuickJSError::new_err(format!("resolve call failed: {}", e))
            })?;
            Ok(())
        })
    }

    /// §7.4: called by the Python driving loop to reject a
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
            let _: Value<'_> = reject.call((exc.into_value(),)).map_err(|e| {
                QuickJSError::new_err(format!("reject call failed: {}", e))
            })?;
            Ok(())
        })
    }

    /// §7.4: Promise state — 0=pending, 1=fulfilled, 2=rejected,
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

    /// §7.4: return a QjsHandle to the Promise's settled value
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
            let promise = val.as_promise().ok_or_else(|| {
                QuickJSError::new_err("promise_result on a non-promise handle")
            })?;
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

    /// §7.4: drain QuickJS's job queue (Promise reactions).
    /// Returns negative on job-error, otherwise the count of jobs
    /// executed. The driving loop polls this between
    /// promise-state checks.
    fn run_pending_jobs(&self) -> PyResult<i32> {
        let runtime = self.context()?.runtime().clone();
        let mut count: i32 = 0;
        loop {
            match runtime.execute_pending_job() {
                Ok(true) => count += 1,
                Ok(false) => break,
                Err(_) => {
                    // Job-level exception: the reaction threw.
                    // QuickJS reports this via a return value; we
                    // surface it as a negative count so the Python
                    // driving loop can decide whether to raise.
                    count = -1;
                    break;
                }
            }
        }
        Ok(count)
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
