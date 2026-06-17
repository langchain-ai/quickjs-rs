//! Fine-grained WASM execution plane — guest.
//!
//! Reactor-mode wasm32-wasip1 module exposing a flat, quickjs-wasi-shaped
//! export set, implemented with rquickjs. Handles are raw pointers to boxed
//! `Persistent<Value>`.
//!
//! Module map:
//!   - `mem`       — qjs_alloc/qjs_free + the result buffer + read_input.
//!   - `engine`    — single Runtime+Context per instance + status codes +
//!                   limits/interrupt/memory-usage.
//!   - `handles`   — value construction + typed accessors + handle ops +
//!                   `eval_code` (the sole sync eval; returns a handle, like
//!                   quickjs-wasi's evalCode). The `new_*` constructors and
//!                   their symmetric `get_*` accessors live together here.
//!   - `error`     — the error channel: `last_exception`/`new_error`/`throw`.
//!   - `hostfn`    — `new_function` + the `host_call` trampoline.
//!   - `promise`   — deferred promises, job pump, `eval_async`.
//!   - `modules`   — synchronous host-backed Resolver/Loader + `eval_module`.
//!
//! ## Eval surface (one sync, one async — matching quickjs-wasi)
//! There is a single sync eval, `eval_code`, which returns a HANDLE to the
//! result (the host reads it via the typed accessors in `handles`). The fused
//! "eval-and-dump-by-value" convenience export was removed: it returned a
//! value instead of a handle, breaking the handle-oriented model, and was a
//! `-spec` codec-plane artifact with no place on the fine-grained ABI.
//! `eval_async` (promise) is the promise-driven counterpart.
//!
//! ## Boundary discipline
//! Every export is `catch_unwind`-guarded: a panic becomes an error status,
//! never an unwind across the wasm boundary. Untrusted `(ptr, len)` and the
//! `argv` handle array are validated before use.

mod engine;
mod error;
mod handles;
mod hostfn;
mod mem;
mod modules;
mod promise;
