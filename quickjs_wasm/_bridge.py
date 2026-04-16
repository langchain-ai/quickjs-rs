"""wasmtime wiring. See spec/implementation.md §7, §9.

Internal: loads quickjs.wasm, stubs WASI imports (denied by default per §9),
implements host_call and host_interrupt, and exposes a thin Python-side
wrapper over the qjs_* shim exports used by the public API.

The surface is grown incrementally as assertions in tests/test_smoke.py
turn green. Exports that the shim itself stubs to -1 are still callable
here — the public API is responsible for surfacing the error.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from importlib import resources
from typing import Any

import wasmtime

from quickjs_wasm import _msgpack
from quickjs_wasm.errors import (
    HostError,
    JSError,
    MemoryLimitError,
    QuickJSError,
    TimeoutError,
)

# Cadence at which the engine epoch ticks. Every Context.eval sets an
# epoch deadline in ticks — timeout_seconds / _EPOCH_TICK_SECONDS — so the
# wasmtime side traps at roughly the same wall-clock moment the QuickJS
# host_interrupt would. §9: this is the backup path in case a C-level
# loop in QuickJS's own code ever bypasses the interrupt hook.
_EPOCH_TICK_SECONDS = 0.05

_WASM_FILE = "quickjs.wasm"


def _load_module(engine: wasmtime.Engine) -> wasmtime.Module:
    resource = resources.files("quickjs_wasm._resources").joinpath(_WASM_FILE)
    with resources.as_file(resource) as path:
        if not path.exists():
            raise RuntimeError(
                f"{_WASM_FILE} is missing from quickjs_wasm/_resources/. "
                "Run ./wasm/build.sh or install a released wheel."
            )
        return wasmtime.Module.from_file(engine, str(path))


class Bridge:
    """One wasm instance. Owns a Runtime + one Store.

    Spec §9: WASI is denied by default. The instance gets an empty
    WasiConfig (no preopens, no env, no stdio passthrough) so reads fail
    and writes go nowhere. Later commits will intercept specific WASI
    calls to enforce the documented behavior per §9 — for now the default
    wasi-common deny-everything baseline is enough to run `1 + 2`.
    """

    def __init__(self) -> None:
        engine_config = wasmtime.Config()
        engine_config.epoch_interruption = True
        self._engine = wasmtime.Engine(engine_config)
        self._module = _load_module(self._engine)

        config = wasmtime.WasiConfig()
        self._store = wasmtime.Store(self._engine)
        self._store.set_wasi(config)
        # Initialize the store's epoch deadline to "effectively never"
        # so module load and one-off shim calls don't trap. Context.eval
        # overwrites this around each user-facing call.
        self._store.set_epoch_deadline(1 << 32)

        linker = wasmtime.Linker(self._engine)
        linker.define_wasi()

        # host_interrupt: QuickJS calls this periodically during execution.
        # We return 1 when the per-context deadline has elapsed, which
        # makes QuickJS throw an uncatchable InternalError("interrupted")
        # that the Python side recognizes as TimeoutError. §7.3, §9.
        linker.define_func(
            "env",
            "host_interrupt",
            wasmtime.FuncType([], [wasmtime.ValType.i32()]),
            self._host_interrupt_check,
        )

        # env::host_call — dispatched when JS calls a host-registered
        # function. Signature: (fn_id, args_ptr, args_len, out_ptr, out_len)
        # -> int32. See §6.3.
        i32 = wasmtime.ValType.i32()
        linker.define_func(
            "env",
            "host_call",
            wasmtime.FuncType([i32, i32, i32, i32, i32], [i32]),
            self._host_call_dispatch,
        )

        # fn_id → Python callable. Populated by Bridge.register_host_function.
        self._host_fns: dict[int, Callable[..., Any]] = {}
        self._next_fn_id = 1
        # Instrumentation so tests can assert the copy-out-first invariant
        # (see §9 re-entrancy note). Each dispatch increments.
        self._host_call_counter = 0
        # Side-channel for HostError.__cause__ threading (§10.2): the most
        # recent Python exception raised inside a host dispatch, waiting
        # to be picked up by Context._raise_from_exception_slot. Cleared
        # after consumption so a second eval can't see a stale cause.
        self._last_host_exception: BaseException | None = None

        # Per-call deadline in monotonic seconds. Set by Context.eval at
        # entry; host_interrupt compares time.monotonic() to this value.
        # None means "no deadline" (e.g. shim bookkeeping calls).
        self._deadline: float | None = None

        # Daemon thread that increments the engine epoch at _EPOCH_TICK_SECONDS
        # cadence. Without this, set_epoch_deadline would never fire since
        # nothing else advances the engine's epoch counter.
        self._shutdown = threading.Event()
        self._epoch_thread = threading.Thread(
            target=self._tick_epoch, name="quickjs-wasm-epoch", daemon=True
        )
        self._epoch_thread.start()

        self._instance = linker.instantiate(self._store, self._module)

        exports = self._instance.exports(self._store)
        self._memory: wasmtime.Memory = exports["memory"]  # type: ignore[assignment]
        self._exports: dict[str, wasmtime.Func] = {}
        for name in _EXPORT_NAMES:
            fn = exports.get(name)
            if fn is None:
                raise RuntimeError(f"quickjs.wasm is missing export {name!r}")
            self._exports[name] = fn  # type: ignore[assignment]

    def close(self) -> None:
        """Stop the epoch-ticker thread. Runtime.close() calls this."""
        self._shutdown.set()

    def _tick_epoch(self) -> None:
        while not self._shutdown.wait(_EPOCH_TICK_SECONDS):
            self._engine.increment_epoch()

    def _host_interrupt_check(self) -> int:
        deadline = self._deadline
        if deadline is None:
            return 0
        return 1 if time.monotonic() >= deadline else 0

    def set_deadline(self, deadline: float | None) -> None:
        """Set the wall-clock deadline seen by host_interrupt, plus the
        wasmtime epoch deadline as a backup path §9 mandates."""
        self._deadline = deadline
        if deadline is None:
            self._store.set_epoch_deadline(1 << 32)
            return
        remaining = max(deadline - time.monotonic(), 0.0)
        # Round up and add one tick of slack so the wall-clock check
        # normally fires first (producing the nicer InterruptError path)
        # and the epoch trap is reserved for genuine bypass cases.
        ticks = int(remaining / _EPOCH_TICK_SECONDS) + 2
        self._store.set_epoch_deadline(ticks)

    # ---- Memory helpers -------------------------------------------------

    def read_bytes(self, ptr: int, length: int) -> bytes:
        return bytes(self._memory.read(self._store, ptr, ptr + length))

    def write_bytes(self, ptr: int, data: bytes | bytearray) -> None:
        self._memory.write(self._store, data, ptr)

    # ---- Raw call helper ------------------------------------------------

    def _call(self, name: str, *args: Any) -> Any:
        return self._exports[name](self._store, *args)

    # ---- Shim exports ---------------------------------------------------

    def malloc(self, size: int) -> int:
        return int(self._call("qjs_malloc", size))

    def free(self, ptr: int) -> None:
        if ptr:
            self._call("qjs_free", ptr)

    def runtime_new(self) -> int:
        return int(self._call("qjs_runtime_new"))

    def runtime_free(self, rt: int) -> None:
        self._call("qjs_runtime_free", rt)

    def runtime_set_memory_limit(self, rt: int, limit: int) -> None:
        self._call("qjs_runtime_set_memory_limit", rt, limit)

    def runtime_set_stack_limit(self, rt: int, limit: int) -> None:
        self._call("qjs_runtime_set_stack_limit", rt, limit)

    def runtime_install_interrupt(self, rt: int) -> None:
        self._call("qjs_runtime_install_interrupt", rt)

    def context_new(self, rt: int) -> int:
        return int(self._call("qjs_context_new", rt))

    def context_free(self, ctx: int) -> None:
        self._call("qjs_context_free", ctx)

    def slot_drop(self, ctx: int, slot: int) -> None:
        if slot:
            self._call("qjs_slot_drop", ctx, slot)

    def eval(self, ctx: int, code: str, flags: int = 0) -> tuple[int, int]:
        """Return (status, slot). status: 0 ok, 1 exception, <0 shim error."""
        encoded = code.encode("utf-8")
        code_ptr = self.malloc(len(encoded)) if encoded else 0
        if encoded and code_ptr == 0:
            raise MemoryError("guest OOM allocating eval code buffer")
        out_ptr = self.malloc(4)
        if out_ptr == 0:
            self.free(code_ptr)
            raise MemoryError("guest OOM allocating eval out-slot pointer")
        try:
            if encoded:
                self.write_bytes(code_ptr, encoded)
            status = int(
                self._call("qjs_eval", ctx, code_ptr, len(encoded), flags, out_ptr)
            )
            slot_bytes = self.read_bytes(out_ptr, 4)
            slot = int.from_bytes(slot_bytes, "little")
            return status, slot
        finally:
            self.free(code_ptr)
            self.free(out_ptr)

    def to_msgpack(self, ctx: int, slot: int) -> tuple[int, bytes]:
        """Return (status, payload). Payload is empty on non-zero status."""
        out_ptr = self.malloc(8)
        if out_ptr == 0:
            raise MemoryError("guest OOM allocating to_msgpack out-params")
        try:
            status = int(self._call("qjs_to_msgpack", ctx, slot, out_ptr, out_ptr + 4))
            if status != 0:
                return status, b""
            header = self.read_bytes(out_ptr, 8)
            data_ptr = int.from_bytes(header[0:4], "little")
            data_len = int.from_bytes(header[4:8], "little")
            return 0, self.read_bytes(data_ptr, data_len)
        finally:
            self.free(out_ptr)

    def exception_to_msgpack(self, ctx: int, exc_slot: int) -> tuple[int, bytes]:
        """Return (status, {name, message, stack} msgpack payload)."""
        out_ptr = self.malloc(8)
        if out_ptr == 0:
            raise MemoryError("guest OOM allocating exception_to_msgpack out-params")
        try:
            status = int(
                self._call(
                    "qjs_exception_to_msgpack", ctx, exc_slot, out_ptr, out_ptr + 4
                )
            )
            if status != 0:
                return status, b""
            header = self.read_bytes(out_ptr, 8)
            data_ptr = int.from_bytes(header[0:4], "little")
            data_len = int.from_bytes(header[4:8], "little")
            return 0, self.read_bytes(data_ptr, data_len)
        finally:
            self.free(out_ptr)

    def take_last_host_exception(self) -> BaseException | None:
        """Pop the most recent Python exception raised in a host dispatch.

        Context.eval calls this after catching a JS HostError so the
        raised HostError.__cause__ can point at the original traceback.
        """
        exc = self._last_host_exception
        self._last_host_exception = None
        return exc

    def raise_from_exception_slot(self, ctx: int, exc_slot: int) -> None:
        """Extract {name, message, stack} off a JS exception slot and raise.

        Single source of truth for §10.1 + §10.2 routing:

        - InternalError + "out of memory" → MemoryLimitError
        - InternalError + "interrupted"   → TimeoutError
        - name == "HostError"             → HostError with __cause__
                                            threaded from the bridge's
                                            side-channel (§10.2)
        - everything else                 → JSError(name, message, stack)

        Does not drop ``exc_slot`` — the caller owns its lifetime and
        disposes it in a finally so this helper can be called from both
        Context.eval's "drop after" path and from contexts that want to
        keep the slot alive for debugging (none today, but the
        responsibility split keeps options open).
        """
        status, payload = self.exception_to_msgpack(ctx, exc_slot)
        if status < 0:
            raise QuickJSError(
                f"shim error from qjs_exception_to_msgpack: status={status}"
            )
        record = _msgpack.decode(payload)
        if not isinstance(record, dict):
            raise QuickJSError(
                f"qjs_exception_to_msgpack returned {type(record).__name__}, "
                "expected dict"
            )
        name = record.get("name") or "Error"
        message = record.get("message") or ""
        stack = record.get("stack")
        if not isinstance(name, str):
            name = str(name)
        if not isinstance(message, str):
            message = str(message)
        stack_str: str | None = stack if isinstance(stack, str) else None

        # §10.1 InternalError routing — markers confirmed against
        # quickjs-ng v0.14.0 (quickjs.c:8082 / quickjs.c:8167).
        if name == "InternalError":
            if "out of memory" in message:
                raise MemoryLimitError(message)
            if "interrupted" in message:
                raise TimeoutError(message)

        if name == "HostError":
            cause = self.take_last_host_exception()
            err = HostError(name, message, stack_str)
            if cause is not None:
                raise err from cause
            raise err
        raise JSError(name, message, stack_str)

    def from_msgpack(self, ctx: int, payload: bytes) -> tuple[int, int]:
        """Return (status, slot). status: 0 ok, <0 shim error."""
        data_ptr = self.malloc(len(payload)) if payload else 0
        if payload and data_ptr == 0:
            raise MemoryError("guest OOM allocating from_msgpack input")
        out_ptr = self.malloc(4)
        if out_ptr == 0:
            self.free(data_ptr)
            raise MemoryError("guest OOM allocating from_msgpack out-slot")
        try:
            if payload:
                self.write_bytes(data_ptr, payload)
            status = int(
                self._call("qjs_from_msgpack", ctx, data_ptr, len(payload), out_ptr)
            )
            slot = int.from_bytes(self.read_bytes(out_ptr, 4), "little")
            return status, slot
        finally:
            self.free(data_ptr)
            self.free(out_ptr)

    def get_global_object(self, ctx: int) -> int:
        out_ptr = self.malloc(4)
        if out_ptr == 0:
            raise MemoryError("guest OOM allocating get_global_object out-slot")
        try:
            status = int(self._call("qjs_get_global_object", ctx, out_ptr))
            if status != 0:
                return 0
            return int.from_bytes(self.read_bytes(out_ptr, 4), "little")
        finally:
            self.free(out_ptr)

    def _write_key(self, key: str) -> tuple[int, int]:
        encoded = key.encode("utf-8")
        key_ptr = self.malloc(len(encoded)) if encoded else 0
        if encoded and key_ptr == 0:
            raise MemoryError("guest OOM allocating property key")
        if encoded:
            self.write_bytes(key_ptr, encoded)
        return key_ptr, len(encoded)

    def get_prop(self, ctx: int, obj_slot: int, key: str) -> tuple[int, int]:
        """Return (status, slot). status: 0 ok, 1 JS exception, <0 shim error."""
        key_ptr, key_len = self._write_key(key)
        out_ptr = self.malloc(4)
        if out_ptr == 0:
            self.free(key_ptr)
            raise MemoryError("guest OOM allocating get_prop out-slot")
        try:
            status = int(
                self._call("qjs_get_prop", ctx, obj_slot, key_ptr, key_len, out_ptr)
            )
            slot = int.from_bytes(self.read_bytes(out_ptr, 4), "little")
            return status, slot
        finally:
            self.free(key_ptr)
            self.free(out_ptr)

    def set_prop(self, ctx: int, obj_slot: int, key: str, val_slot: int) -> int:
        """Return status. 0 = ok, 1 = JS exception, <0 = shim error."""
        key_ptr, key_len = self._write_key(key)
        try:
            return int(
                self._call("qjs_set_prop", ctx, obj_slot, key_ptr, key_len, val_slot)
            )
        finally:
            self.free(key_ptr)

    def call(
        self,
        ctx: int,
        fn_slot: int,
        this_slot: int,
        arg_slots: list[int],
    ) -> tuple[int, int]:
        """Return (status, result_slot). status: 0 ok, 1 JS exception, <0 shim error."""
        argc = len(arg_slots)
        argv_ptr = 0
        if argc > 0:
            argv_ptr = self.malloc(4 * argc)
            if argv_ptr == 0:
                raise MemoryError("guest OOM allocating call argv")
            payload = b"".join(s.to_bytes(4, "little") for s in arg_slots)
            self.write_bytes(argv_ptr, payload)
        out_ptr = self.malloc(4)
        if out_ptr == 0:
            if argv_ptr:
                self.free(argv_ptr)
            raise MemoryError("guest OOM allocating call out-slot")
        try:
            status = int(
                self._call(
                    "qjs_call", ctx, fn_slot, this_slot, argc, argv_ptr, out_ptr
                )
            )
            slot = int.from_bytes(self.read_bytes(out_ptr, 4), "little")
            return status, slot
        finally:
            if argv_ptr:
                self.free(argv_ptr)
            self.free(out_ptr)

    def type_of(self, ctx: int, slot: int) -> int:
        return int(self._call("qjs_type_of", ctx, slot))

    def slot_dup(self, ctx: int, slot: int) -> int:
        return int(self._call("qjs_slot_dup", ctx, slot))

    # ---- Host-function registry -----------------------------------------

    def register_host_function(
        self, ctx: int, name: str, fn: Callable[..., Any]
    ) -> None:
        fn_id = self._next_fn_id
        self._next_fn_id += 1
        self._host_fns[fn_id] = fn
        name_ptr, name_len = self._write_key(name)
        try:
            rc = int(
                self._call(
                    "qjs_register_host_function", ctx, name_ptr, name_len, fn_id
                )
            )
            if rc != 0:
                del self._host_fns[fn_id]
                raise RuntimeError(
                    f"qjs_register_host_function failed: status={rc}"
                )
        finally:
            self.free(name_ptr)

    def _host_call_dispatch(
        self,
        fn_id: int,
        args_ptr: int,
        args_len: int,
        out_ptr_addr: int,
        out_len_addr: int,
    ) -> int:
        """env::host_call implementation. See §6.3.

        Returns 0 on host-side success (reply is a marshaled value) or 1 on
        host-side raise (reply is a {name, message, stack} record). Negative
        returns are reserved for marshaling failures.
        """
        self._host_call_counter += 1

        # §9 re-entrancy: copy args out of guest memory immediately. The
        # shim owns the args buffer for the duration of this call, but if
        # the Python callable synchronously calls back into ctx.eval the
        # shim's scratch will be reused — so snapshot now, decode after.
        args_payload = self.read_bytes(args_ptr, args_len) if args_len else b""

        fn = self._host_fns.get(fn_id)
        if fn is None:
            reply = _msgpack.encode(
                {
                    "name": "HostError",
                    "message": f"no host function registered with fn_id={fn_id}",
                    "stack": None,
                }
            )
            return self._write_host_reply(out_ptr_addr, out_len_addr, reply, status=1)

        try:
            args = _msgpack.decode(args_payload)
            if not isinstance(args, list):
                raise TypeError(
                    f"host_call args must decode to a list, got {type(args).__name__}"
                )
            result = fn(*args)
            reply = _msgpack.encode(result)
            return self._write_host_reply(out_ptr_addr, out_len_addr, reply, status=0)
        except BaseException as exc:  # noqa: BLE001 — bridge across languages
            # Stash the live Python exception so Context.eval can attach
            # it as HostError.__cause__ if/when the JS side surfaces the
            # error back to Python. Overwritten on each dispatch; cleared
            # when Context consumes it.
            self._last_host_exception = exc
            reply = _msgpack.encode(
                {
                    "name": "HostError",
                    "message": str(exc),
                    "stack": "".join(traceback.format_exception(exc)),
                }
            )
            return self._write_host_reply(out_ptr_addr, out_len_addr, reply, status=1)

    def _write_host_reply(
        self, out_ptr_addr: int, out_len_addr: int, reply: bytes, status: int
    ) -> int:
        """Allocate a guest buffer via qjs_malloc, write the reply, store
        (ptr, len) at the two shim-provided out-pointers. §6.3 stipulates
        the shim qjs_free's the buffer after reading."""
        if reply:
            ptr = self.malloc(len(reply))
            if ptr == 0:
                return -1
            self.write_bytes(ptr, reply)
        else:
            ptr = 0
        self.write_bytes(out_ptr_addr, ptr.to_bytes(4, "little"))
        self.write_bytes(out_len_addr, len(reply).to_bytes(4, "little"))
        return status


_EXPORT_NAMES: tuple[str, ...] = (
    "qjs_runtime_new",
    "qjs_runtime_free",
    "qjs_runtime_set_memory_limit",
    "qjs_runtime_set_stack_limit",
    "qjs_runtime_install_interrupt",
    "qjs_context_new",
    "qjs_context_free",
    "qjs_slot_dup",
    "qjs_slot_drop",
    "qjs_eval",
    "qjs_to_msgpack",
    "qjs_from_msgpack",
    "qjs_exception_to_msgpack",
    "qjs_get_global_object",
    "qjs_get_prop",
    "qjs_set_prop",
    "qjs_call",
    "qjs_type_of",
    "qjs_register_host_function",
    "qjs_malloc",
    "qjs_free",
)


__all__ = ["Bridge"]
