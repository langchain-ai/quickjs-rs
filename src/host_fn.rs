//! §6.5 / §7.4 host function plumbing.
//!
//! Two trampolines — sync and async — installed as JS functions on
//! globalThis. Each captures a Python dispatcher callable (fn_id
//! registry lookup lives Python-side) and does the JS↔Python
//! marshaling on the way in and out.
//!
//! The async trampoline additionally manages the Promise /
//! (resolve, reject) pair that backs each in-flight async host
//! call: creates the Promise via `ctx.promise()`, stashes the
//! resolver pair keyed by a fresh pending_id, calls the Python
//! async dispatcher with (fn_id, args, pending_id), and returns
//! the Promise synchronously. Python then schedules an asyncio
//! task and calls `resolve_pending` / `reject_pending` on the
//! QjsContext to settle the Promise.
//!
//! Why these live as free functions: bare closures in the
//! `register_host_function` body split the `'js` lifetime into
//! independent inferred lifetimes across the Ctx arg, Rest<Value>
//! args, and Value return — those don't unify against the
//! `Fn(Ctx<'js>, Rest<Value<'js>>) -> Value<'js>` bound rquickjs
//! needs. Routing through a top-level generic function lets Rust
//! infer one shared `'js`.

use pyo3::prelude::*;
use pyo3::types::PyAny;
use rquickjs::{atom::PredefinedAtom, Ctx, Function, Persistent, Value};
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;

use crate::errors::QuickJSError;
use crate::marshal::{js_value_to_py, py_to_js_value};

/// Stored resolver pair for an in-flight async host call Promise.
/// §6.5 / §7.4: the Rust trampoline creates a Promise via
/// `ctx.promise()`, stashes (resolve_fn, reject_fn) keyed by
/// `pending_id`, and calls back into Python with (fn_id, args,
/// pending_id). When the Python async task completes, it calls
/// `resolve_pending` / `reject_pending` which restores these
/// functions and invokes them with the settlement value.
pub(crate) struct PendingResolver {
    pub(crate) resolve: Persistent<Function<'static>>,
    pub(crate) reject: Persistent<Function<'static>>,
}

/// Build and install a host function trampoline. Lives as a free
/// function so Rust can infer the closure lifetime as a single
/// `'js` threading through the Ctx arg, Rest<Value> args, and Value
/// return.
pub(crate) fn build_host_trampoline<'js>(
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

/// Build an async host function trampoline. See module docs.
#[allow(clippy::too_many_arguments)]
pub(crate) fn build_async_host_trampoline<'js>(
    ctx: &Ctx<'js>,
    dispatcher: Py<PyAny>,
    fn_id: u32,
    name: &str,
    pending: Rc<RefCell<HashMap<u32, PendingResolver>>>,
    next_id: Rc<Cell<u32>>,
    sync_hit: Rc<Cell<bool>>,
    in_sync: Rc<Cell<bool>>,
) -> PyResult<Function<'js>> {
    let trampoline = move |cx: Ctx<'js>,
                           args: rquickjs::function::Rest<Value<'js>>|
          -> rquickjs::Result<Value<'js>> {
        dispatch_async_host_fn(
            &cx,
            &dispatcher,
            fn_id,
            args.0,
            &pending,
            &next_id,
            &sync_hit,
            &in_sync,
        )
    };
    Function::new(ctx.clone(), trampoline)
        .map_err(|e| QuickJSError::new_err(format!("Function::new failed: {}", e)))?
        .with_name(name)
        .map_err(|e| QuickJSError::new_err(format!("Function::with_name failed: {}", e)))
}

/// Shared body for the async host trampoline. On entry:
///   1. Create a JS Promise + resolver pair via `ctx.promise()`.
///   2. If we're inside a sync eval, set the sync_hit flag and
///      immediately reject the promise with a sentinel so JS sees
///      a rejected promise rather than hanging. Python's sync-eval
///      path consumes the sync_hit flag on return and raises
///      ConcurrentEvalError before the rejection matters.
///   3. Otherwise, allocate a fresh pending_id, stash
///      (resolve, reject) keyed by it, marshal args to Python,
///      call the dispatcher with (fn_id, args, pending_id). The
///      dispatcher returns 0 on successful scheduling or negative
///      on error — on error, pop the entry and reject locally.
///   4. Return the Promise synchronously to JS.
#[allow(clippy::too_many_arguments)]
fn dispatch_async_host_fn<'js>(
    ctx: &Ctx<'js>,
    dispatcher: &Py<PyAny>,
    fn_id: u32,
    args: Vec<Value<'js>>,
    pending: &Rc<RefCell<HashMap<u32, PendingResolver>>>,
    next_id: &Rc<Cell<u32>>,
    sync_hit: &Rc<Cell<bool>>,
    in_sync: &Rc<Cell<bool>>,
) -> rquickjs::Result<Value<'js>> {
    let (promise, resolve, reject) = ctx.promise()?;

    // §7.4: sync-eval hit async host fn. Set the flag so the
    // Python sync-eval surface raises ConcurrentEvalError; also
    // reject the Promise locally so the JS expression evaluates
    // to a rejected Promise rather than never settling. The sync
    // eval returns before any driving loop could observe the
    // rejection, so this is effectively just bookkeeping.
    if in_sync.get() {
        sync_hit.set(true);
        let exc = rquickjs::Exception::from_message(
            ctx.clone(),
            "async host fn dispatched from sync eval",
        )?;
        let _ = exc
            .as_object()
            .set(PredefinedAtom::Name, "ConcurrentEvalError");
        let _: Value<'_> = reject.call((exc.into_value(),))?;
        return Ok(promise.into_value());
    }

    // Allocate pending_id and stash the resolver pair.
    let pid = next_id.get();
    next_id.set(pid.wrapping_add(1));
    let entry = PendingResolver {
        resolve: Persistent::save(ctx, resolve),
        reject: Persistent::save(ctx, reject.clone()),
    };
    pending.borrow_mut().insert(pid, entry);

    // Marshal args to Python and call the dispatcher.
    let rc: i32 = Python::attach(|py| -> rquickjs::Result<i32> {
        let py_args: Vec<Py<PyAny>> = args
            .iter()
            .map(|v| js_value_to_py(py, v, 0))
            .collect::<PyResult<Vec<_>>>()
            .map_err(|e| rquickjs::Error::IntoJs {
                from: "python",
                to: "js",
                message: Some(format!("arg marshal failed: {}", e)),
            })?;
        let args_tuple = pyo3::types::PyTuple::new(py, &py_args).map_err(|e| {
            rquickjs::Error::IntoJs {
                from: "python",
                to: "js",
                message: Some(format!("arg tuple build failed: {}", e)),
            }
        })?;
        let result = dispatcher
            .bind(py)
            .call1((fn_id, args_tuple, pid))
            .map_err(|e| rquickjs::Error::IntoJs {
                from: "python",
                to: "js",
                message: Some(format!("async dispatcher raised: {}", e)),
            })?;
        Ok(result.extract::<i32>().unwrap_or(0))
    })?;

    if rc < 0 {
        // Dispatcher signaled failure (typically a registration
        // mismatch). Pop the entry and reject locally with a
        // HostError so JS sees a clean rejection rather than a
        // dangling pending Promise.
        if let Some(entry) = pending.borrow_mut().remove(&pid) {
            let resolve = entry.resolve.restore(ctx)?;
            let reject_fn = entry.reject.restore(ctx)?;
            let _ = resolve; // drop to free
            let exc = rquickjs::Exception::from_message(
                ctx.clone(),
                "Host function failed",
            )?;
            let _ = exc.as_object().set(PredefinedAtom::Name, "HostError");
            let _: Value<'_> = reject_fn.call((exc.into_value(),))?;
        }
    }

    Ok(promise.into_value())
}

/// Trampoline invoked by rquickjs when JS calls a registered sync
/// host function. Marshals args to Python, calls the dispatcher,
/// marshals the return value back to JS. On Python exception from
/// the dispatcher, constructs a JS Error whose name/message come
/// from the PyErr's type and args tuple (the Python dispatcher
/// re-raises host-fn exceptions as
/// `_engine.JSError(name="HostError", message, stack)`), throws it,
/// and returns `Error::Exception` to unwind into JS.
fn call_host_fn<'js>(
    ctx: &Ctx<'js>,
    dispatcher: &Py<PyAny>,
    fn_id: u32,
    args: Vec<Value<'js>>,
) -> rquickjs::Result<Value<'js>> {
    // GIL is already held — we're inside ctx.eval which was called
    // from Python. Step 5 never releases the GIL during eval; step
    // 9 may reconsider for async. `attach` is cheap when already
    // held.
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
/// an infra-level Python exception — construct a generic Error
/// with the PyErr's string representation.
fn throw_host_error(ctx: &Ctx<'_>, err: &PyErr, py: Python<'_>) -> rquickjs::Error {
    let (name, message) = extract_jserror_fields(err, py)
        .unwrap_or_else(|| ("Error".to_string(), err.to_string()));

    // Build the JS Error object via Exception::from_message, then
    // set .name to whatever the dispatcher requested.
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
    // Rust side's _engine.JSError is `create_exception!` — args live
    // on `.args`. The Python-side dispatcher re-raises _engine.JSError
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
