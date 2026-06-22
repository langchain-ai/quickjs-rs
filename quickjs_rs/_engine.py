"""wasmtime-py-backed engine for quickjs-rs (the WASM execution plane).

This replaces the PyO3-compiled ``_engine`` extension with a pure-Python host
adapter driving the fine-grained WASM guest (``crates/quickjs-wasm/``). It presents
the same surface the public ``quickjs_rs`` package consumes (``QjsRuntime``, ``QjsContext``,
``QjsHandle``, ``UNDEFINED``, and the engine-side exception classes), so the
coupled public modules (``handle.py``/``context.py``/``runtime.py``) sit on top
with minimal change (the "Option B" port).

Design notes:
  - One guest wasm instance per :class:`QjsRuntime`. The guest is one
    Runtime+Context per instance; :class:`QjsContext` is a thin
    facade over that single guest context (a multi-context model layers on
    later — for now one context per runtime, which the tests exercise).
  - Handles are opaque guest ``i32`` pointers (raw ptr to a boxed
    ``Persistent<Value>``). :class:`QjsHandle` wraps one + a disposed flag.
  - Host-call dispatch is BY NAME — the Python dispatcher is keyed by
    the registered function name, not a numeric id.
  - Marshalling sits here, on top of the fine-grained ops: a Python value →
    ``new_*``/``set_*`` crossings; a guest value → typed-accessor reads.

Security: every ``(ptr, len)`` read out of guest memory is bound-checked
against ``memory.data_len`` before slicing. The ``host_call`` name is an
untrusted, guest-controlled lookup key (opaque dict key; unknown → error
sentinel). Lengths are capped. The wasm sandbox is the isolation boundary.
"""

from __future__ import annotations

import struct
import threading
import weakref
from collections.abc import Callable
from typing import Any, cast

import wasmtime

from quickjs_rs._transform import SourceTransformer, TransformError
from quickjs_rs._wasmtime import (
    SharedWasmArtifact,
    instantiate_wasm_artifact,
    shared_wasm_artifact,
    shared_wasmtime_engine,
)

_BUNDLED_WASM = "_guest.wasm"  # package-data filename inside quickjs_rs/


def _read_guest_wasm() -> bytes:
    """Read the bundled guest wasm bytes (quickjs_rs/_guest.wasm)."""
    from importlib.resources import files

    res = files("quickjs_rs") / _BUNDLED_WASM
    if not res.is_file():
        raise QuickJSError(
            f"guest wasm not found (quickjs_rs/{_BUNDLED_WASM}). "
            "Build it with `python scripts/build_guest.py`."
        )
    return res.read_bytes()


# --------------------------------------------------------------------------
# JSMemoryUsage fields IN STRUCT ORDER (mirror crates/quickjs-wasm/src/engine.rs
# compute_memory_usage, which writes 26 i64 LE in this exact order). Struct
# order is the single source of truth — no per-field tags cross the boundary.
# --------------------------------------------------------------------------
_MEMORY_USAGE_FIELDS = (
    "malloc_size",
    "malloc_limit",
    "memory_used_size",
    "malloc_count",
    "memory_used_count",
    "atom_count",
    "atom_size",
    "str_count",
    "str_size",
    "obj_count",
    "obj_size",
    "prop_count",
    "prop_size",
    "shape_count",
    "shape_size",
    "js_func_count",
    "js_func_size",
    "js_func_code_size",
    "js_func_pc2line_count",
    "js_func_pc2line_size",
    "c_func_count",
    "array_count",
    "fast_array_count",
    "fast_array_elements",
    "binary_object_count",
    "binary_object_size",
)

# --------------------------------------------------------------------------
# Whole-memory snapshot envelope. The header guards a snapshot
# against the WRONG guest build: build_id = sha256(wasm) catches every
# layout-affecting rebuild, format_version guards the envelope layout itself.
# --------------------------------------------------------------------------
SNAP_MAGIC = b"QFGS"  # QuickJS Fine-Grained Snapshot
SNAP_FORMAT_VERSION = 1
# magic(4) + format_version(4) + build_id(32) + memory_size(8) + stack_pointer(4)
_SNAP_HEADER = struct.Struct("<4sI32sQI")

# Guest status codes (mirror crates/quickjs-wasm/src/engine.rs).
STATUS_OK = 0
STATUS_JS_ERROR = 1
STATUS_BAD_INPUT = 2
STATUS_PANIC = 3
STATUS_NO_ENGINE = 4

# Host-call sentinel (mirror crates/quickjs-wasm/src/hostfn.rs).
HOST_CALL_ERROR = -1

# Null handle sentinel (mirror crates/quickjs-wasm/src/handles.rs).
NULL_HANDLE = 0

# type_of returns the canonical type-name STRING directly from the guest (the
# single source of truth) — no host-side numeric-tag table to drift.

# Marshalling recursion cap (cycle / deep-nesting guard) — matches the prior
# PyO3 engine's MAX_MARSHAL_DEPTH so the documented behavior is preserved.
_MAX_MARSHAL_DEPTH = 128

# Sanitized message for a host-function failure surfaced into JS (no host
# internals leak).
_HOST_ERROR_SANITIZED_MESSAGE = "host function raised an exception"


# --------------------------------------------------------------------------
# Engine-side exception classes. These mirror the names the PyO3 ``.pyi``
# exported; the public ``quickjs_rs.errors`` classes are distinct and the
# public wrappers translate between them. Keeping these here means
# ``handle.py``'s ``except _engine.JSError`` etc. keep working unchanged.
# --------------------------------------------------------------------------


class QuickJSError(Exception): ...


class JSError(QuickJSError):
    """A JS exception. ``args == (name, message, stack)``."""


class MarshalError(QuickJSError): ...


class InvalidHandleError(QuickJSError): ...


# --------------------------------------------------------------------------
# UNDEFINED sentinel (distinct from None; preserved inside containers).
# --------------------------------------------------------------------------


class Undefined:
    """Singleton sentinel for JS ``undefined`` (distinct from ``null``→None)."""

    _instance: Undefined | None = None

    def __new__(cls) -> Undefined:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Undefined)

    def __hash__(self) -> int:
        return hash(Undefined)


UNDEFINED = Undefined()


# --------------------------------------------------------------------------
# Shared compilation + host-import domain. The guest wasm is JIT-compiled by
# Cranelift once (~60ms) and the result reused for EVERY instance. The Linker
# (carrying the 4 host-function imports) is ALSO shared and built exactly once.
#
# Why share the Linker — and why the thread-local. wasmtime-py registers every
# host function in a process-global, UNLOCKED slab (`FUNCTIONS` in
# wasmtime/_func.py): `define_func` mutates it on registration and `Store.close()`
# mutates it on teardown, with no synchronization. Registering host functions
# PER INSTANCE therefore races that slab under concurrent context create/close
# across threads — corrupting it (spurious `TypeError: list indices must be
# integers ...` / `WasmtimeError`), and the corruption is process-wide and
# sticky. The fix is to touch that slab a CONSTANT number of times for the whole
# process: register the 4 imports ONCE on a shared Linker, then every instance
# just `instantiate()`s it (instantiate does not touch the slab).
#
# A single shared host-function closure cannot capture per-instance `self`, so it
# must learn which `_Instance` is calling some other way. `_CUR` — a thread-local
# — names the instance whose export is currently running on this thread.
# `_Instance.c()` sets `_CUR.inst = self` around every guest call; host imports
# only ever fire re-entrantly DURING such a call, on the same thread, so the
# thread-local always resolves to the right instance. Per-thread storage means
# concurrent instances on different threads never collide.
# --------------------------------------------------------------------------

# Per-thread pointer to the `_Instance` whose export is currently executing; the
# shared host-import trampolines read `_CUR.inst` to route back to it (see the
# domain note above and `_Instance.c()`).
_CUR = threading.local()


# Module-level trampoline shims registered ONCE on the shared Linker. Each
# forwards to the method on the currently-active instance (`_CUR.inst`), which
# carries the real per-instance dispatch state (name_to_fn_id, sync/async
# dispatchers, memory, store). The method bodies (and their bound-checked reads /
# host-error sanitization) stay on `_Instance`; these shims only route.
def _tramp_host_call(name_ptr: int, name_len: int, this: int, argc: int, argv_ptr: int) -> int:
    return cast("_Instance", _CUR.inst)._host_call(name_ptr, name_len, this, argc, argv_ptr)


def _tramp_host_interrupt() -> int:
    return cast("_Instance", _CUR.inst)._host_interrupt()


def _tramp_host_module_normalize(
    base_ptr: int, base_len: int, spec_ptr: int, spec_len: int, out_len_ptr: int
) -> int:
    return cast("_Instance", _CUR.inst)._host_module_normalize(
        base_ptr, base_len, spec_ptr, spec_len, out_len_ptr
    )


def _tramp_host_module_load(name_ptr: int, name_len: int, out_len_ptr: int) -> int:
    return cast("_Instance", _CUR.inst)._host_module_load(name_ptr, name_len, out_len_ptr)


def _build_shared_linker(engine: wasmtime.Engine) -> wasmtime.Linker:
    """Build the process-shared Linker, registering the 4 host-function imports
    exactly once (so wasmtime-py's global slab is touched a constant number of
    times — see the domain note above)."""
    linker = wasmtime.Linker(engine)
    linker.define_wasi()
    i32 = wasmtime.ValType.i32
    # env.host_call(name_ptr, name_len, this, argc, argv_ptr) -> ret_handle
    linker.define_func(
        "env", "host_call", wasmtime.FuncType([i32()] * 5, [i32()]), _tramp_host_call
    )
    # env.host_interrupt() -> i32 (nonzero = stop). Hot-loop poll.
    linker.define_func(
        "env", "host_interrupt", wasmtime.FuncType([], [i32()]), _tramp_host_interrupt
    )
    # env.host_module_normalize(base_ptr, base_len, spec_ptr, spec_len, out_len_ptr) -> name_ptr
    linker.define_func(
        "env",
        "host_module_normalize",
        wasmtime.FuncType([i32()] * 5, [i32()]),
        _tramp_host_module_normalize,
    )
    # env.host_module_load(name_ptr, name_len, out_len_ptr) -> source_ptr
    linker.define_func(
        "env",
        "host_module_load",
        wasmtime.FuncType([i32()] * 3, [i32()]),
        _tramp_host_module_load,
    )
    return linker


def _quickjs_artifact() -> SharedWasmArtifact:
    """Lazily return the process-shared QuickJS artifact."""
    return shared_wasm_artifact(
        "quickjs",
        _read_guest_wasm,
        _build_shared_linker,
    )


# --------------------------------------------------------------------------
# The wasm instance: store/linker/exports + memory helpers + env imports.
# One per QjsContext.
# --------------------------------------------------------------------------


class _Instance:
    """A loaded guest wasm instance and the low-level call surface."""

    def __init__(self, *, memory_limit: int | None = None, stack_limit: int | None = None) -> None:
        # Share the Engine + compiled Module + Linker (see the domain note
        # above); only the Store is per-instance. Host imports are registered
        # ONCE on the shared Linker — we do NOT define_func per instance (that
        # raced wasmtime-py's global slab). The shared trampolines route back
        # here via the `_CUR` thread-local set in `c()`.
        artifact = _quickjs_artifact()
        self.build_id = artifact.build_id
        self.store = wasmtime.Store(shared_wasmtime_engine())
        wasi = wasmtime.WasiConfig()
        wasi.inherit_stdout()
        wasi.inherit_stderr()
        self.store.set_wasi(wasi)

        # Interrupt poll: a callable () -> bool the host_interrupt import calls
        # from the guest's hot loop (set by QjsRuntime; None = never interrupt).
        # MUST be O(1) — it fires very frequently during JS execution.
        self.interrupt_handler: Callable[[], bool] | None = None

        # Instantiate the SHARED precompiled module via the SHARED linker (no
        # recompile, no per-instance import registration — ~0.3ms).
        self.inst = instantiate_wasm_artifact(artifact, self.store)
        e = self.inst.exports(self.store)
        # wasmtime's exports[name] is a union (Func | Memory | Global | ...);
        # we know the concrete kinds for these named exports.
        self.mem = cast(wasmtime.Memory, e["memory"])
        # __stack_pointer global — captured/restored as part of a snapshot (§6).
        self.sp = cast(wasmtime.Global, e["__stack_pointer"])
        self._exports = e
        # Set true once a module-mode eval runs — module snapshot is V1-guarded.
        self.module_touched = False

        # Host-call dispatch. The GUEST dispatches by NAME; the
        # public context.py layer dispatches by a numeric fn_id. We bridge:
        # `name_to_fn_id` translates the guest's name back to the id the
        # sync/async dispatchers expect, so context.py stays unchanged.
        self.sync_dispatch: Callable[[int, tuple[Any, ...]], Any] | None = None
        self.async_dispatch: Callable[[int, tuple[Any, ...], int], int] | None = None
        self.name_to_fn_id: dict[str, int] = {}
        self.async_names: set[str] = set()  # names registered is_async=True
        # Sync-eval-hits-async guard: context.py sets in_sync_eval before a
        # SYNC eval/call; if an async host fn fires during it (no event loop to
        # drive it → would deadlock), _host_call sets sync_eval_hit_async so the
        # caller raises ConcurrentEvalError instead.
        self.in_sync_eval = False
        self.sync_eval_hit_async = False
        # Host module loader, set via QjsRuntime.set_module_loader.
        # normalize(base, spec) -> canonical name (default identity); load(name)
        # -> source. Both may return None to signal unresolvable / not-found
        # (the guest surfaces a clean JS error).
        self.module_normalize: Callable[[str, str], str | None] | None = None
        self.module_load: Callable[[str], str | None] | None = None
        self.module_transform_flags: int | Callable[[str], int] | None = None
        self.module_transformer = SourceTransformer()
        self._closed = False

    def close(self) -> None:
        """Release the wasm instance.

        The `_Instance` is kept alive by a reference cycle through native code:
        the host-function callbacks (`self._host_call`, …) capture `self` and
        are stored in wasmtime's host-function table, reachable from `self.inst`
        — so `_Instance → inst → wasmtime table → bound method → _Instance`.
        Plain refcounting can't collect that; it would wait on the cyclic GC.

        `wasmtime.Store.close()` is the operation that frees the store's native
        state (the linear-memory reservation) AND drops those held callbacks,
        which breaks the cycle so the `_Instance` then collects normally. That's
        all that's needed — the other handles (`inst`/`mem`/`sp`) become inert
        (use raises `ValueError`) and are reclaimed with the wrapper. Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.module_transformer.close()
        finally:
            self.store.close()

    # -- raw export call --
    def c(self, name: str, *args: Any) -> Any:
        # Every named entry here is a function export; wasmtime types the
        # lookup as a union, so narrow to Func before calling.
        fn = cast(wasmtime.Func, self._exports[name])
        # Publish THIS instance as the active one for the duration of the guest
        # call, so the shared Linker's import trampolines (registered once,
        # process-wide) route their host callbacks back to us via `_CUR`. Host
        # imports only fire re-entrantly DURING a guest call on this same thread,
        # so the per-thread `_CUR.inst` always names the right instance. Save +
        # restore (not just set + clear) because host callbacks re-enter `c()`
        # (e.g. _host_call → python_to_handle → c("new_null")); restoring the
        # previous value keeps nested calls correct.
        prev = getattr(_CUR, "inst", None)
        _CUR.inst = self
        try:
            return fn(self.store, *args)
        finally:
            _CUR.inst = prev

    # -- memory: bound-checked reads, writes via guest alloc --
    def _mem_size(self) -> int:
        return self.mem.data_len(self.store)

    def read(self, ptr: int, length: int) -> bytes:
        if ptr < 0 or length < 0 or ptr + length > self._mem_size():
            raise QuickJSError(f"out-of-range guest read ptr={ptr} len={length}")
        return bytes(self.mem.read(self.store, ptr, ptr + length))

    def alloc_write(self, data: bytes) -> int:
        p = self.c("qjs_alloc", len(data))
        if p == 0 and len(data) > 0:
            raise QuickJSError("qjs_alloc returned null")
        if data:
            self.mem.write(self.store, data, p)
        return int(p)

    def free(self, ptr: int, length: int) -> None:
        self.c("qjs_free", ptr, length)

    # -- out-param slots --
    def alloc_out(self, size: int = 4) -> int:
        return int(self.c("qjs_alloc", size))

    def read_i32(self, ptr: int) -> int:
        return int(struct.unpack("<i", self.read(ptr, 4))[0])

    def read_u32(self, ptr: int) -> int:
        return int(struct.unpack("<I", self.read(ptr, 4))[0])

    def read_u64(self, ptr: int) -> int:
        return int(struct.unpack("<Q", self.read(ptr, 8))[0])

    # -- result buffer (one-slot channel for get_string/get_bigint/...) --
    def take_result(self) -> bytes:
        n = self.c("qjs_last_len")
        buf = self.read(self.c("qjs_last_ptr"), n) if n else b""
        self.c("qjs_result_free")
        return buf

    # -- the env.host_call import body (guest dispatches by NAME) --
    def _host_call(self, name_ptr: int, name_len: int, this: int, argc: int, argv_ptr: int) -> int:
        try:
            name = self.read(name_ptr, name_len).decode()
            fn_id = self.name_to_fn_id.get(name)
            if fn_id is None:
                return HOST_CALL_ERROR
            args = []
            if argc:
                raw = self.read(argv_ptr, 4 * argc)
                for h in struct.unpack(f"<{argc}i", raw):
                    # Each arg handle is owned by the guest for the call; read
                    # it by value (the dispatcher gets Python values).
                    args.append(self.handle_to_python(h, allow_opaque=False))

            # ASYNC host fn dispatched during a SYNC eval/call → there is no
            # event loop to drive the coroutine; flag it so the caller raises
            # ConcurrentEvalError, and fail this call.
            if name in self.async_names and self.in_sync_eval:
                self.sync_eval_hit_async = True
                return HOST_CALL_ERROR

            # ASYNC host fn: create a deferred (reentrant new_promise — safe via
            # the guest's CURRENT_RAW_CTX reentrancy), hand the deferred id to
            # context.py's async dispatcher as the pending_id, and return the
            # PROMISE handle. The coroutine settles it later via resolve/reject_
            # pending (drive loop drains microtasks between settles).
            if name in self.async_names and self.async_dispatch is not None:
                promise_handle, deferred_id = self._new_deferred()
                if promise_handle == NULL_HANDLE:
                    return HOST_CALL_ERROR
                rc = self.async_dispatch(fn_id, tuple(args), deferred_id)
                if rc != 0:
                    # Scheduling failed → reject the deferred now so the promise
                    # doesn't dangle, and still return it (JS sees a rejection).
                    self._reject_deferred_generic(deferred_id)
                return promise_handle

            # SYNC host fn.
            if self.sync_dispatch is None:
                return HOST_CALL_ERROR
            result = self.sync_dispatch(fn_id, tuple(args))
            return self.python_to_handle(result)
        except Exception:
            # ANY dispatcher failure → return the error sentinel and let the
            # GUEST synthesize a generic sanitized HostError. The host authors
            # NO JS-visible string on the failure path (no host→guest leak). The
            # original exception (incl. a host JSError carrying detail) stays
            # host-side; context.py's side channel recognizes the guest's
            # sanitized "HostError"/"Host function failed" and unwraps to the
            # original Python exception at the Python eval boundary — never
            # inside JS.
            return HOST_CALL_ERROR

    # -- interrupt poll --
    def _host_interrupt(self) -> int:
        # Hot-loop poll: keep it O(1). Returns 1 to interrupt (deadline passed).
        h = self.interrupt_handler
        if h is None:
            return 0
        try:
            return 1 if h() else 0
        except Exception:
            # A failing handler must not wedge the guest loop; treat as "stop"
            # so a broken poll fails closed rather than spinning forever.
            return 1

    # -- deferred promises (async host calls) --
    def _new_deferred(self) -> tuple[int, int]:
        """Create a guest deferred. Returns (promise_handle, deferred_id).
        The deferred_id IS used as context.py's pending_id (one namespace)."""
        id_out = self.c("qjs_alloc", 4)
        try:
            promise = self.c("new_promise", id_out)
            deferred_id = struct.unpack("<I", self.read(id_out, 4))[0]
        finally:
            self.c("qjs_free", id_out, 4)
        return promise, deferred_id

    def resolve_deferred(self, deferred_id: int, value: Any) -> None:
        vh = self.python_to_handle(value)
        # resolve_deferred takes ownership of the value handle.
        self.c("resolve_deferred", deferred_id, vh)

    def reject_deferred(self, deferred_id: int, err_handle: int) -> None:
        # reject_deferred takes ownership of the error handle.
        self.c("reject_deferred", deferred_id, err_handle)

    def _reject_deferred_generic(self, deferred_id: int) -> None:
        """Reject with a generic sanitized HostError (scheduling failure path)."""
        err = self._make_host_error()
        self.c("reject_deferred", deferred_id, err)

    def _make_host_error(self) -> int:
        """Mint a sanitized HostError handle (name=HostError, fixed message).
        Used for async rejection — same wholesale-sanitized policy as the sync
        path (no host string crosses; the original stays host-side)."""
        data = b"Host function failed"
        p = self.alloc_write(data)
        out = self.alloc_out()
        try:
            st = self.c("new_error", p, len(data), out)
            err = self.read_i32(out) if st == STATUS_OK else NULL_HANDLE
        finally:
            self.free(p, len(data))
            self.free(out, 4)
        if err != NULL_HANDLE:
            try:
                nh = self._new_string("HostError")
                try:
                    self._set_prop(err, "name", nh)
                finally:
                    self.c("free_value", nh)
            except Exception:
                pass
        return err

    # -- module loader imports (host moduleLoader callbacks) --
    def _host_module_normalize(
        self, base_ptr: int, base_len: int, spec_ptr: int, spec_len: int, out_len_ptr: int
    ) -> int:
        try:
            base = self.read(base_ptr, base_len).decode() if base_len else ""
            spec = self.read(spec_ptr, spec_len).decode() if spec_len else ""
            if self.module_normalize is not None:
                canonical = self.module_normalize(base, spec)
            else:
                canonical = spec  # identity default
            if canonical is None:
                return 0
            data = str(canonical).encode()
            p = self.alloc_write(data)
            self.mem.write(self.store, struct.pack("<I", len(data)), out_len_ptr)
            return p
        except Exception:
            return 0

    def _host_module_load(self, name_ptr: int, name_len: int, out_len_ptr: int) -> int:
        try:
            name = self.read(name_ptr, name_len).decode()
            if self.module_load is None:
                return 0
            source = self.module_load(name)
            if source is None:
                return 0
            data = self.module_transformer.transform(
                name,
                str(source),
                flags=self._module_transform_flags(name),
            ).encode()
            p = self.alloc_write(data)
            self.mem.write(self.store, struct.pack("<I", len(data)), out_len_ptr)
            return p
        except TransformError:
            return 0
        except Exception:
            return 0

    def _module_transform_flags(self, name: str) -> int | None:
        policy = self.module_transform_flags
        if policy is None:
            # SourceTransformer treats None as "use the default module policy";
            # an explicit 0/SourceTransform.NONE disables transforms.
            return None
        if callable(policy):
            return int(policy(name))
        return int(policy)

    # ----------------------------------------------------------------------
    # Marshalling: Python value -> guest handle.
    # ----------------------------------------------------------------------

    def python_to_handle(self, value: Any, _depth: int = 0) -> int:
        if _depth > _MAX_MARSHAL_DEPTH:
            raise MarshalError("recursion limit exceeded while marshaling")
        # An already-minted guest handle (QjsHandle) passes through.
        if isinstance(value, QjsHandle):
            return value._require_live()
        if value is None:
            return int(self.c("new_null"))
        if isinstance(value, Undefined):
            return int(self.c("new_undefined"))
        if isinstance(value, bool):
            return int(self.c("new_bool", 1 if value else 0))
        if isinstance(value, int):
            # JS-safe integers cross as numbers; anything wider as BigInt
            # (decimal string — never truncate).
            if -(2**53) <= value <= 2**53:
                return int(self.c("new_number", _f64_bits(float(value))))
            return self._new_bigint(str(value))
        if isinstance(value, float):
            return int(self.c("new_number", _f64_bits(value)))
        if isinstance(value, str):
            return self._new_string(value)
        if isinstance(value, (bytes, bytearray)):
            return self._new_uint8array(bytes(value))
        if isinstance(value, (list, tuple)):
            arr = self.c("new_array")
            for i, item in enumerate(value):
                ih = self.python_to_handle(item, _depth + 1)
                st = self.c("set_index", arr, i, ih)
                self.c("free_value", ih)
                if st != STATUS_OK:
                    raise MarshalError(f"set_index failed at {i}")
            return int(arr)
        if isinstance(value, dict):
            obj = self.c("new_object")
            for k, v in value.items():
                if not isinstance(k, str):
                    raise MarshalError("only string keys are supported")
                vh = self.python_to_handle(v, _depth + 1)
                self._set_prop(obj, k, vh)
                self.c("free_value", vh)
            return int(obj)
        raise MarshalError(f"cannot marshal {type(value).__name__}")

    def _new_string(self, s: str) -> int:
        data = s.encode()
        p = self.alloc_write(data)
        try:
            return int(self.c("new_string", p, len(data)))
        finally:
            self.free(p, len(data))

    def _new_bigint(self, decimal: str) -> int:
        data = decimal.encode()
        p = self.alloc_write(data)
        out = self.alloc_out()
        try:
            st = self.c("new_bigint", p, len(data), out)
            if st != STATUS_OK:
                raise MarshalError(f"new_bigint status={st}")
            return self.read_i32(out)
        finally:
            self.free(p, len(data))
            self.free(out, 4)

    def _new_uint8array(self, data: bytes) -> int:
        p = self.alloc_write(data)
        out = self.alloc_out()
        try:
            st = self.c("new_uint8array", p, len(data), out)
            if st != STATUS_OK:
                raise MarshalError(f"new_uint8array status={st}")
            return self.read_i32(out)
        finally:
            self.free(p, len(data))
            self.free(out, 4)

    def _set_prop(self, obj: int, key: str, value_handle: int) -> None:
        data = key.encode()
        p = self.alloc_write(data)
        try:
            st = self.c("set_prop", obj, p, len(data), value_handle)
            if st != STATUS_OK:
                raise MarshalError(f"set_prop({key!r}) status={st}")
        finally:
            self.free(p, len(data))

    # ----------------------------------------------------------------------
    # Marshalling: guest handle -> Python value (typed accessors, NOT dump).
    # ----------------------------------------------------------------------

    def handle_to_python(self, handle: int, *, allow_opaque: bool, _depth: int = 0) -> Any:
        if _depth > _MAX_MARSHAL_DEPTH:
            raise MarshalError(
                f"recursion limit of {_MAX_MARSHAL_DEPTH} exceeded while "
                "marshaling (cycle or deeply nested structure)"
            )
        name = self._type_name(handle)

        if name == "undefined":
            # Root undefined coerces to None; nested stays UNDEFINED.
            return None if _depth == 0 else UNDEFINED
        if name == "null":
            return None
        if name == "boolean":
            return self._get_bool(handle)
        if name == "number":
            f = self._get_number(handle)
            # Narrow integer-valued floats to int (matches prior engine).
            if f == f and f.is_integer() and abs(f) < 2**53:
                return int(f)
            return f
        if name == "bigint":
            return int(self._get_bigint(handle))
        if name == "string":
            return self._get_string(handle)
        if name == "array":
            return self._array_to_list(handle, allow_opaque, _depth)
        if name == "object":
            # A Promise is an object structurally, but not marshalable by value
            # (its eventual value isn't known here) — treat it as opaque, like
            # function/symbol. Otherwise it would dict-ify to {} silently.
            if self._is_promise(handle):
                if allow_opaque:
                    return QjsHandle._adopt(self, self.c("dup_handle", handle))
                raise MarshalError("cannot marshal a JS promise to Python")
            # Uint8Array / ArrayBuffer special-case to bytes.
            ab = self._try_arraybuffer(handle)
            if ab is not None:
                return ab
            return self._object_to_dict(handle, allow_opaque, _depth)
        # function / symbol / proxy / unknown: opaque.
        if allow_opaque:
            return QjsHandle._adopt(self, self.c("dup_handle", handle))
        raise MarshalError(f"cannot marshal a JS {name} to Python")

    def _is_promise(self, handle: int) -> bool:
        out = self.alloc_out()
        try:
            st = self.c("is_promise", handle, out)
            return st == STATUS_OK and self.read_i32(out) != 0
        finally:
            self.free(out, 4)

    def _array_to_list(self, handle: int, allow_opaque: bool, depth: int) -> list[Any]:
        n = self._array_length(handle)
        out: list[Any] = []
        for i in range(n):
            ih = self._get_index(handle, i)
            try:
                out.append(self.handle_to_python(ih, allow_opaque=allow_opaque, _depth=depth + 1))
            finally:
                self.c("free_value", ih)
        return out

    def _object_to_dict(self, handle: int, allow_opaque: bool, depth: int) -> dict[str, Any]:
        keys_h = self._own_property_names(handle)
        try:
            n = self._array_length(keys_h)
            result: dict[str, Any] = {}
            for i in range(n):
                kh = self._get_index(keys_h, i)
                try:
                    key = self._get_string(kh)
                finally:
                    self.c("free_value", kh)
                vh = self._get_prop(handle, key)
                try:
                    result[key] = self.handle_to_python(
                        vh, allow_opaque=allow_opaque, _depth=depth + 1
                    )
                finally:
                    self.c("free_value", vh)
            return result
        finally:
            self.c("free_value", keys_h)

    # -- typed accessor wrappers --
    def _type_name(self, handle: int) -> str:
        st = self.c("type_of", handle)
        if st != STATUS_OK:
            raise InvalidHandleError(f"type_of status={st}")
        return self.take_result().decode()

    def _get_number(self, handle: int) -> float:
        out = self.c("qjs_alloc", 8)
        try:
            st = self.c("get_number", handle, out)
            if st != STATUS_OK:
                raise MarshalError(f"get_number status={st}")
            return float(struct.unpack("<d", struct.pack("<Q", self.read_u64(out)))[0])
        finally:
            self.free(out, 8)

    def _get_bool(self, handle: int) -> bool:
        out = self.alloc_out()
        try:
            st = self.c("get_bool", handle, out)
            if st != STATUS_OK:
                raise MarshalError(f"get_bool status={st}")
            return self.read_i32(out) != 0
        finally:
            self.free(out, 4)

    def _get_string(self, handle: int) -> str:
        st = self.c("get_string", handle)
        if st != STATUS_OK:
            raise MarshalError(f"get_string status={st}")
        return self.take_result().decode()

    def _get_bigint(self, handle: int) -> str:
        st = self.c("get_bigint", handle)
        if st != STATUS_OK:
            raise MarshalError(f"get_bigint status={st}")
        return self.take_result().decode()

    def _try_arraybuffer(self, handle: int) -> bytes | None:
        """Return bytes if the handle is an ArrayBuffer or Uint8Array, else None."""
        # Try a Uint8Array view first (covers the common bytes case).
        o1, o2, o3 = self.alloc_out(), self.alloc_out(), self.alloc_out()
        try:
            st = self.c("get_typed_array_buffer", handle, o1, o2, o3)
            if st == STATUS_OK:
                return self.take_result()
        finally:
            for o in (o1, o2, o3):
                self.free(o, 4)
        # Then a raw ArrayBuffer.
        st = self.c("get_arraybuffer", handle)
        if st == STATUS_OK:
            return self.take_result()
        return None

    def _get_prop(self, obj: int, key: str) -> int:
        data = key.encode()
        p = self.alloc_write(data)
        out = self.alloc_out()
        try:
            st = self.c("get_prop", obj, p, len(data), out)
            if st != STATUS_OK:
                raise JSError("Error", f"get_prop({key!r}) status={st}", None)
            return self.read_i32(out)
        finally:
            self.free(p, len(data))
            self.free(out, 4)

    def _get_index(self, obj: int, index: int) -> int:
        out = self.alloc_out()
        try:
            st = self.c("get_index", obj, index, out)
            if st != STATUS_OK:
                raise JSError("Error", f"get_index status={st}", None)
            return self.read_i32(out)
        finally:
            self.free(out, 4)

    def _own_property_names(self, obj: int) -> int:
        out = self.alloc_out()
        try:
            st = self.c("get_own_property_names", obj, out)
            if st != STATUS_OK:
                raise JSError("Error", f"get_own_property_names status={st}", None)
            return self.read_i32(out)
        finally:
            self.free(out, 4)

    def _array_length(self, arr: int) -> int:
        lh = self._get_prop(arr, "length")
        try:
            return int(self._get_number(lh))
        finally:
            self.c("free_value", lh)

    # -- error channel --
    def take_exception(self) -> JSError:
        """After a STATUS_JS_ERROR, take the pending exception and build a
        :class:`JSError` with (name, message, stack).

        An Error *object* contributes its `.name`/`.message`/`.stack`. A
        non-Error throw (`throw 42`, `throw 'x'`) has no such fields — it
        coerces via `String(v)` to the message, with name="Error", stack=None
        """
        out = self.alloc_out()
        try:
            st = self.c("last_exception", out)
            if st != STATUS_OK:
                return JSError("Error", "unknown JS error", None)
            exc = self.read_i32(out)
        finally:
            self.free(out, 4)
        try:
            # An Error object has a string `.name`; a bare value does not.
            name = self._read_error_field(exc, "name")
            if name is None:
                # Non-Error throw: coerce the whole value to the message.
                coerced = self._coerce_to_string(exc)
                return JSError("Error", coerced, None)
            message = self._read_error_field(exc, "message") or ""
            stack = self._read_error_field(exc, "stack")
            return JSError(name, message, stack)
        finally:
            self.c("free_value", exc)

    def _coerce_to_string(self, handle: int) -> str:
        """Coerce a thrown non-Error value to its message string (≈ `String(v)`).
        `throw 42` → "42", `throw 'x'` → "x"."""
        try:
            name = self._type_name(handle)
            if name == "string":
                return self._get_string(handle)
            if name == "bigint":
                return self._get_bigint(handle)
            py = self.handle_to_python(handle, allow_opaque=False)
            if py is None:
                return "null"
            if isinstance(py, bool):
                return "true" if py else "false"
            return str(py)
        except Exception:
            return ""

    def _read_error_field(self, exc: int, field: str) -> str | None:
        try:
            fh = self._get_prop(exc, field)
        except Exception:
            return None
        try:
            name = self._type_name(fh)
            if name == "string":
                return self._get_string(fh)
            if name in ("undefined", "null"):
                return None
            # Coerce non-string (e.g. throw 42) via str of its python form.
            return str(self.handle_to_python(fh, allow_opaque=False))
        except Exception:
            return None
        finally:
            self.c("free_value", fh)

    # -- whole-memory snapshot / restore --
    def snapshot(self) -> bytes:
        """Capture the entire guest heap: full linear memory + __stack_pointer.
        Everything QuickJS allocates lives in
        linear memory by offset, so a wholesale copy preserves closures, pending
        promises, aliasing, and raw-pointer handles. The wasm code is NOT
        included (it's the fixed substrate to restore into)."""
        size = self.mem.data_len(self.store)
        image = bytes(self.mem.read(self.store, 0, size))
        sp_val = self.sp.value(self.store)
        header = _SNAP_HEADER.pack(SNAP_MAGIC, SNAP_FORMAT_VERSION, self.build_id, size, sp_val)
        return header + image

    def restore(self, snap: bytes, *, write: bool = True) -> None:
        """Validate the snapshot header FAIL-CLOSED IN ORDER, then (if `write`)
        overwrite this instance's linear memory with the image + set the stack
        pointer. `write=False` validates only (inject_globals=False path)."""
        if len(snap) < _SNAP_HEADER.size:
            raise QuickJSError("snapshot shorter than header")
        magic, fmt, build_id, mem_size, sp_val = _SNAP_HEADER.unpack_from(snap, 0)
        if magic != SNAP_MAGIC:
            raise ValueError(f"bad snapshot magic {magic!r}")
        if fmt != SNAP_FORMAT_VERSION:
            raise ValueError(
                f"unsupported snapshot format version {fmt} "
                f"(this build expects {SNAP_FORMAT_VERSION})"
            )
        if build_id != self.build_id:
            raise ValueError(
                "snapshot build_id mismatch — it was taken from a different "
                "guest wasm build (rebuilding the guest invalidates snapshots)"
            )
        image = snap[_SNAP_HEADER.size :]
        if len(image) != mem_size:
            raise ValueError(f"snapshot image length {len(image)} != header memory_size {mem_size}")
        if sp_val > mem_size:
            raise ValueError(f"snapshot stack_pointer {sp_val} out of range")
        if not write:
            return
        # All checks passed → write. A fresh instance starts at the minimum and
        # only grows; mem_size < current is impossible and would be rejected,
        # never truncated.
        cur = self.mem.data_len(self.store)
        if mem_size > cur:
            page = 65536
            self.mem.grow(self.store, (mem_size - cur + page - 1) // page)
        self.mem.write(self.store, image, 0)
        self.sp.set_value(self.store, sp_val)


def _f64_bits(f: float) -> int:
    return int(struct.unpack("<Q", struct.pack("<d", f))[0])


# --------------------------------------------------------------------------
# QjsRuntime — owns the wasm instance.
# --------------------------------------------------------------------------


class QjsRuntime:
    """Config holder + factory for contexts. NOT a QuickJS-style runtime.

    Important reframing for the wasm plane: in native QuickJS a `JSRuntime` is a
    shared GC heap that multiple `JSContext`s live in (and can share objects
    across). **That concept does not exist here.** The unit of isolation on the
    wasm plane is the *instance* — one linear memory = one QuickJS runtime+context
    welded together ("one VM = one instance"). There is no sub-instance
    "context sharing a heap."

    So this class is just the config/factory the existing public API expects: it
    holds what's genuinely shared across the contexts a caller mints from it — the
    memory/stack limits, the module source registry, the interrupt handler — and
    hands each `QjsContext` its OWN isolated wasm instance. Two contexts on the
    same `QjsRuntime` therefore have fully independent globals and cannot leak
    state to each other (they are separate VMs, not a shared heap). The public
    `Runtime`/`Context` split is preserved for API/test compatibility; collapsing
    it is a deliberate later (user-facing) decision.
    """

    def __init__(self, *, memory_limit: int | None = None, stack_limit: int | None = None) -> None:
        self._memory_limit = memory_limit
        self._stack_limit = stack_limit
        self._closed = False
        self._interrupt_handler: Callable[[], Any] | None = None
        self._module_normalize: Callable[[str, str], str | None] | None = None
        self._module_load: Callable[[str], str | None] | None = None
        self._module_transform_flags: int | Callable[[str], int] | None = None
        self._instances: weakref.WeakSet[_Instance] = weakref.WeakSet()

    def _new_instance(self) -> _Instance:
        """Create a fresh, isolated wasm instance for a new context, seeded
        with this runtime's module loader + limits + interrupt poll."""
        inst = _Instance(memory_limit=self._memory_limit, stack_limit=self._stack_limit)
        inst.module_normalize = self._module_normalize
        inst.module_load = self._module_load
        inst.module_transform_flags = self._module_transform_flags
        inst.interrupt_handler = self._interrupt_handler
        # Apply resource limits BEFORE any eval (the first eval creates the
        # guest runtime, which reads these). set_*_limit is idempotent.
        if self._memory_limit is not None:
            inst.c("set_memory_limit", self._memory_limit)
        if self._stack_limit is not None:
            inst.c("set_max_stack_size", self._stack_limit)
        self._instances.add(inst)
        return inst

    def set_interrupt_handler(self, handler: Callable[[], Any]) -> None:
        # The runtime.py _interrupt closure: () -> bool, reads self._deadline.
        # Route it to every existing + future instance's hot-loop poll.
        self._interrupt_handler = handler
        for inst in self._instances:
            inst.interrupt_handler = handler

    def clear_interrupt_handler(self) -> None:
        self._interrupt_handler = None
        for inst in self._instances:
            inst.interrupt_handler = None

    def run_gc(self) -> None:
        """Run QuickJS cycle GC on every live context's heap. Each context is
        its own wasm instance with its own heap, so a runtime-level GC fans out
        across all of them. No-op if no context is live."""
        for inst in self._instances:
            inst.c("run_gc")

    def memory_usage(self) -> dict[str, int]:
        """Aggregate QuickJS memory counters across every live context's heap.

        Each context is its own wasm instance with its own QuickJS runtime, so
        there is no single runtime-wide heap; we sum each field across all live
        instances. An empty runtime (no live context) reports all-zeros — the
        honest "no heaps allocated yet" answer, with a stable return shape."""
        sums = [0] * len(_MEMORY_USAGE_FIELDS)
        for inst in self._instances:
            st = inst.c("compute_memory_usage")
            if st != STATUS_OK:
                raise QuickJSError(f"compute_memory_usage status={st}")
            raw = inst.take_result()
            values = struct.unpack(f"<{len(_MEMORY_USAGE_FIELDS)}q", raw)
            for i, v in enumerate(values):
                sums[i] += v
        return dict(zip(_MEMORY_USAGE_FIELDS, sums, strict=True))

    def set_module_loader(
        self,
        *,
        normalize: Callable[[str, str], str | None] | None = None,
        load: Callable[[str], str | None] | None = None,
        transform_flags: int | Callable[[str], int] | None = None,
    ) -> None:
        """Install a host module loader. normalize(base, spec) ->
        canonical name (default identity); load(name) -> source.
        Routed to every existing + future instance."""
        self._module_normalize = normalize
        self._module_load = load
        self._module_transform_flags = transform_flags
        for inst in self._instances:
            inst.module_normalize = normalize
            inst.module_load = load
            inst.module_transform_flags = transform_flags

    def close(self) -> None:
        self._closed = True

    def is_closed(self) -> bool:
        return self._closed


# --------------------------------------------------------------------------
# QjsContext — facade over the single guest context.
# --------------------------------------------------------------------------


class QjsContext:
    def __init__(self, runtime: QjsRuntime) -> None:
        self._runtime = runtime
        # Each context gets its OWN isolated wasm instance.
        self._inst = runtime._new_instance()
        self._closed = False
        # The sync-eval-hits-async guard flags live on the instance (so
        # _host_call can set them); see set_in_sync_eval / _Instance.

    # -- eval --
    def eval(
        self, code: str, *, module: bool = False, strict: bool = False, filename: str = "<eval>"
    ) -> Any:
        handle = self._eval_to_handle(code, module=module)
        try:
            return self._inst.handle_to_python(handle, allow_opaque=False)
        finally:
            self._inst.c("free_value", handle)

    def eval_handle(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        promise: bool = False,
        filename: str = "<eval>",
    ) -> QjsHandle:
        # promise=True → JS_EVAL_FLAG_ASYNC (the guest `eval_async`), which
        # supports top-level await + multi-statement bodies and returns a
        # Promise handle that resolves to the {value} envelope.
        if promise:
            handle = self._eval_async_to_handle(code)
        else:
            handle = self._eval_to_handle(code, module=module)
        return QjsHandle._adopt(self._inst, handle, context=self)

    def eval_module_async(self, code: str, *, filename: str = "<eval>") -> QjsHandle:
        # Module eval returning a Promise (Module::evaluate). Our guest's
        # eval_module evaluates synchronously and returns the namespace; wrap it
        # as an already-settled promise shape by re-evaluating under async eval
        # is not equivalent, so for V1 we evaluate the module and hand back a
        # handle the drive loop treats as settled. context.py expects a Promise;
        # eval_module's result isn't one, so route module-async through the same
        # async eval path which DOES produce a promise.
        handle = self._eval_async_to_handle(code, module=True)
        return QjsHandle._adopt(self._inst, handle, context=self)

    def _eval_to_handle(self, code: str, *, module: bool) -> int:
        data = code.encode()
        inst = self._inst
        p = inst.alloc_write(data)
        out = inst.alloc_out()
        try:
            if module:
                inst.module_touched = True  # module snapshot is V1-guarded
                name = b"<module>"
                np = inst.alloc_write(name)
                try:
                    st = inst.c("eval_module", p, len(data), np, len(name), out)
                finally:
                    inst.free(np, len(name))
            else:
                st = inst.c("eval_code", p, len(data), out)
            handle = inst.read_i32(out)
        finally:
            inst.free(p, len(data))
            inst.free(out, 4)
        if st == STATUS_JS_ERROR:
            raise inst.take_exception()
        if st != STATUS_OK:
            raise JSError("Error", f"eval status={st}", None)
        return handle

    def _eval_async_to_handle(self, code: str, *, module: bool = False) -> int:
        """Return a Promise handle the host drives to completion (settling async
        host calls + draining microtasks). For `module=True` the guest's
        `eval_module_async` compiles+links the module and returns its top-level
        -await promise; otherwise `eval_async` does JS_EVAL_FLAG_ASYNC script
        eval (the {value} envelope)."""
        inst = self._inst
        data = code.encode()
        p = inst.alloc_write(data)
        out = inst.alloc_out()
        try:
            if module:
                inst.module_touched = True  # module snapshot is V1-guarded
                name = b"<module>"
                np = inst.alloc_write(name)
                try:
                    st = inst.c("eval_module_async", p, len(data), np, len(name), out)
                finally:
                    inst.free(np, len(name))
            else:
                st = inst.c("eval_async", p, len(data), out)
            handle = inst.read_i32(out)
        finally:
            inst.free(p, len(data))
            inst.free(out, 4)
        if st == STATUS_JS_ERROR:
            raise inst.take_exception()
        if st != STATUS_OK:
            raise JSError("Error", f"eval_async status={st}", None)
        return handle

    def global_object(self) -> QjsHandle:
        return QjsHandle._adopt(self._inst, self._inst.c("global_object"), context=self)

    # -- host functions --
    # context.py dispatches by a numeric fn_id; the guest dispatches by name.
    # We bridge in _Instance via name_to_fn_id (see _host_call).
    def set_host_call_dispatcher(self, dispatcher: Callable[[int, tuple[Any, ...]], Any]) -> None:
        self._inst.sync_dispatch = dispatcher

    def set_async_host_dispatcher(
        self, dispatcher: Callable[[int, tuple[Any, ...], int], int]
    ) -> None:
        self._inst.async_dispatch = dispatcher

    def register_host_function(self, name: str, fn_id: int, is_async: bool = False) -> None:
        """Mint a guest host function for `name`, install it on globalThis, and
        record name -> fn_id so the by-name guest dispatch reaches the right
        Python callable. `is_async` names take the deferred-promise path in
        `_host_call`."""
        inst = self._inst
        inst.name_to_fn_id[name] = fn_id
        if is_async:
            inst.async_names.add(name)
        else:
            inst.async_names.discard(name)
        data = name.encode()
        p = inst.alloc_write(data)
        try:
            fn_handle = inst.c("new_function", p, len(data))
        finally:
            inst.free(p, len(data))
        # Install on globalThis under `name` so JS resolves the identifier.
        g = inst.c("global_object")
        kp = inst.alloc_write(data)
        try:
            st = inst.c("set_prop", g, kp, len(data), fn_handle)
            if st != STATUS_OK:
                raise JSError("Error", f"install host fn status={st}", None)
        finally:
            inst.free(kp, len(data))
            inst.c("free_value", g)
            inst.c("free_value", fn_handle)

    # -- sync-eval guard flags (live on the instance so _host_call can set) --
    def set_in_sync_eval(self, value: bool) -> None:
        self._inst.in_sync_eval = value

    def take_sync_eval_hit_async_call(self) -> bool:
        hit = self._inst.sync_eval_hit_async
        self._inst.sync_eval_hit_async = False
        return hit

    # -- promise introspection (used by the async drive loop, later step) --
    def promise_state(self, handle: QjsHandle) -> int:
        inst = self._inst
        out = inst.alloc_out()
        try:
            st = inst.c("promise_state", handle._require_live(), out)
            if st != STATUS_OK:
                raise JSError("Error", f"promise_state status={st}", None)
            return inst.read_i32(out)
        finally:
            inst.free(out, 4)

    def promise_result(self, handle: QjsHandle) -> QjsHandle:
        inst = self._inst
        out = inst.alloc_out()
        try:
            # The status is intentionally ignored: a rejected promise returns
            # STATUS_JS_ERROR but the handle IS the rejection reason. The caller
            # (drive loop) distinguishes resolved vs rejected via promise_state.
            inst.c("promise_result", handle._require_live(), out)
            rh = inst.read_i32(out)
        finally:
            inst.free(out, 4)
        return QjsHandle._adopt(inst, rh, context=self)

    def run_pending_jobs(self) -> int:
        return int(self._inst.c("execute_pending_jobs"))

    # -- whole-memory snapshot --
    @property
    def module_touched(self) -> bool:
        return self._inst.module_touched

    def create_snapshot(self) -> bytes:
        """Whole-memory snapshot of this context's wasm instance."""
        return self._inst.snapshot()

    def restore_snapshot(self, data: bytes, *, write: bool = True) -> None:
        """Validate + (optionally) restore a whole-memory snapshot into this
        context's instance. write=False validates the header only."""
        self._inst.restore(data, write=write)

    # -- async-host-call settlement (pending_id == guest deferred_id) --
    def resolve_pending(self, pending_id: int, value: Any) -> None:
        """Resolve the deferred for an async host call with `value`."""
        self._inst.resolve_deferred(pending_id, value)

    def reject_pending(
        self, pending_id: int, name: str, message: str, stack: str | None = None
    ) -> None:
        """Reject the deferred with a JS Error(name, message). context.py passes
        its already-sanitized ("HostError"/"Host function failed") values here —
        the deliberate, recognized rejection shape, not a host leak."""
        inst = self._inst
        data = (message or "").encode()
        p = inst.alloc_write(data)
        out = inst.alloc_out()
        try:
            st = inst.c("new_error", p, len(data), out)
            err = inst.read_i32(out) if st == STATUS_OK else NULL_HANDLE
        finally:
            inst.free(p, len(data))
            inst.free(out, 4)
        if err != NULL_HANDLE and name:
            try:
                nh = inst._new_string(name)
                try:
                    inst._set_prop(err, "name", nh)
                finally:
                    inst.c("free_value", nh)
            except Exception:
                pass
        inst.reject_deferred(pending_id, err)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._inst is not None:
            self._inst.close()

    def is_closed(self) -> bool:
        return self._closed


# --------------------------------------------------------------------------
# QjsHandle — wraps one guest i32 handle.
# --------------------------------------------------------------------------


class QjsHandle:
    """Engine-side handle wrapper. The public ``Handle`` class wraps this.

    Identity for the cross-context guard is the owning ``QjsContext`` id
    (``context_id``). Each context is its own isolated wasm instance, so a handle
    minted in one context is meaningless in another — the guard rejects it.
    """

    __slots__ = ("_inst", "_handle", "_context", "_disposed")

    # Slot types (declared so the type checker sees the attributes set in
    # `_adopt` via object.__new__). These are annotations only — __slots__
    # above is what actually reserves the storage.
    _inst: _Instance
    _handle: int
    _context: QjsContext | None
    _disposed: bool

    def __init__(self) -> None:  # use _adopt
        raise TypeError("construct via QjsHandle._adopt")

    @classmethod
    def _adopt(
        cls, inst: _Instance, handle: int, *, context: QjsContext | None = None
    ) -> QjsHandle:
        self = object.__new__(cls)
        self._inst = inst
        self._handle = handle
        self._context = context
        self._disposed = False
        return self

    def _require_live(self) -> int:
        if self._disposed:
            raise InvalidHandleError("handle is disposed")
        return self._handle

    @property
    def context_id(self) -> int:
        # Identity for the cross-context guard. Keyed to the owning QjsContext
        # so two public contexts over the same wasm instance are still
        # distinguished (handle.py compares this to reject cross-context use).
        return id(self._context) if self._context is not None else id(self._inst)

    @property
    def type_of(self) -> str:
        return self._inst._type_name(self._require_live())

    def is_promise(self) -> bool:
        inst = self._inst
        out = inst.alloc_out()
        try:
            st = inst.c("is_promise", self._require_live(), out)
            if st != STATUS_OK:
                return False
            return inst.read_i32(out) != 0
        finally:
            inst.free(out, 4)

    def get(self, key: str) -> QjsHandle:
        return QjsHandle._adopt(
            self._inst, self._inst._get_prop(self._require_live(), key), context=self._context
        )

    def get_index(self, index: int) -> QjsHandle:
        return QjsHandle._adopt(
            self._inst, self._inst._get_index(self._require_live(), index), context=self._context
        )

    def set(self, key: str, value: Any) -> None:
        inst = self._inst
        # value may be a QjsHandle (already a guest handle) or a Python value.
        if isinstance(value, QjsHandle):
            vh = value._require_live()
            inst._set_prop(self._require_live(), key, vh)
        else:
            vh = inst.python_to_handle(value)
            try:
                inst._set_prop(self._require_live(), key, vh)
            finally:
                inst.c("free_value", vh)

    def has(self, key: str) -> bool:
        """True iff the property exists AND is not `undefined`.

        This collapses the JS distinction between "own property set to
        undefined" and "not defined" — both read as absent from Python's
        dict-like perspective (matches the prior PyO3 engine + the Globals
        `in` contract). Implemented as: exists (has_prop) and its value's
        type is not "undefined"."""
        inst = self._inst
        data = key.encode()
        p = inst.alloc_write(data)
        out = inst.alloc_out()
        try:
            st = inst.c("has_prop", self._require_live(), p, len(data), out)
            if st != STATUS_OK:
                raise JSError("Error", f"has_prop status={st}", None)
            if inst.read_i32(out) == 0:
                return False
        finally:
            inst.free(p, len(data))
            inst.free(out, 4)
        # Exists — but treat an undefined value as absent.
        vh = inst._get_prop(self._require_live(), key)
        try:
            return inst._type_name(vh) != "undefined"
        finally:
            inst.c("free_value", vh)

    def call(self, *args: Any) -> QjsHandle:
        return self._call(self._require_live(), None, args)

    def call_method(self, name: str, *args: Any) -> QjsHandle:
        method = self._inst._get_prop(self._require_live(), name)
        try:
            return self._call(method, self._require_live(), args)
        finally:
            self._inst.c("free_value", method)

    def new_instance(self, *args: Any) -> QjsHandle:
        return self._construct(self._require_live(), args)

    def _call(self, func: int, this: int | None, args: tuple[Any, ...]) -> QjsHandle:
        inst = self._inst
        this_h = this if this is not None else inst.c("new_undefined")
        owns_this = this is None
        arg_handles, owns = self._marshal_args(args)
        argv_ptr, argv_len = self._write_argv(arg_handles)
        out = inst.alloc_out()
        try:
            st = inst.c("call_function", func, this_h, argv_ptr, len(arg_handles), out)
            rh = inst.read_i32(out)
        finally:
            if argv_len:
                inst.free(argv_ptr, argv_len)
            inst.free(out, 4)
            for h, o in zip(arg_handles, owns, strict=True):
                if o:
                    inst.c("free_value", h)
            if owns_this:
                inst.c("free_value", this_h)
        if st == STATUS_JS_ERROR:
            raise inst.take_exception()
        if st != STATUS_OK:
            raise JSError("Error", f"call status={st}", None)
        return QjsHandle._adopt(inst, rh, context=self._context)

    def _construct(self, func: int, args: tuple[Any, ...]) -> QjsHandle:
        inst = self._inst
        arg_handles, owns = self._marshal_args(args)
        argv_ptr, argv_len = self._write_argv(arg_handles)
        out = inst.alloc_out()
        try:
            st = inst.c("call_constructor", func, argv_ptr, len(arg_handles), out)
            rh = inst.read_i32(out)
        finally:
            if argv_len:
                inst.free(argv_ptr, argv_len)
            inst.free(out, 4)
            for h, o in zip(arg_handles, owns, strict=True):
                if o:
                    inst.c("free_value", h)
        if st == STATUS_JS_ERROR:
            raise inst.take_exception()
        if st != STATUS_OK:
            raise JSError("Error", f"construct status={st}", None)
        return QjsHandle._adopt(inst, rh, context=self._context)

    def _marshal_args(self, args: tuple[Any, ...]) -> tuple[list[int], list[bool]]:
        """Return (handles, owns) — owns[i] True if we minted it and must free."""
        handles: list[int] = []
        owns: list[bool] = []
        for a in args:
            if isinstance(a, QjsHandle):
                handles.append(a._require_live())
                owns.append(False)
            else:
                handles.append(self._inst.python_to_handle(a))
                owns.append(True)
        return handles, owns

    def _write_argv(self, handles: list[int]) -> tuple[int, int]:
        if not handles:
            return 0, 0
        inst = self._inst
        argv_len = 4 * len(handles)
        argv_ptr = inst.c("qjs_alloc", argv_len)
        inst.mem.write(inst.store, struct.pack(f"<{len(handles)}i", *handles), argv_ptr)
        return argv_ptr, argv_len

    def to_python(self, *, allow_opaque: bool = False) -> Any:
        return self._inst.handle_to_python(self._require_live(), allow_opaque=allow_opaque)

    def dup(self) -> QjsHandle:
        return QjsHandle._adopt(
            self._inst, self._inst.c("dup_handle", self._require_live()), context=self._context
        )

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        h, self._handle = self._handle, 0
        if h:
            self._inst.c("free_value", h)

    def is_disposed(self) -> bool:
        return self._disposed
