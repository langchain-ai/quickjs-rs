"""wasmtime wiring. See spec/implementation.md §7, §9.

Internal: loads quickjs.wasm, stubs WASI imports (denied by default per §9),
implements host_call and host_interrupt, and exposes a thin Python-side
wrapper over the qjs_* shim exports used by the public API.

The surface is grown incrementally as assertions in tests/test_smoke.py
turn green. Exports that the shim itself stubs to -1 are still callable
here — the public API is responsible for surfacing the error.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

import wasmtime

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
        self._engine = wasmtime.Engine()
        self._module = _load_module(self._engine)

        config = wasmtime.WasiConfig()
        self._store = wasmtime.Store(self._engine)
        self._store.set_wasi(config)

        linker = wasmtime.Linker(self._engine)
        linker.define_wasi()

        # env::host_interrupt — always return 0 (no interrupt) until
        # per-context timeouts land. §6.3 / §7.3 timeout semantics arrive
        # with a later assertion.
        linker.define_func(
            "env",
            "host_interrupt",
            wasmtime.FuncType([], [wasmtime.ValType.i32()]),
            lambda: 0,
        )

        self._instance = linker.instantiate(self._store, self._module)

        exports = self._instance.exports(self._store)
        self._memory: wasmtime.Memory = exports["memory"]  # type: ignore[assignment]
        self._exports: dict[str, wasmtime.Func] = {}
        for name in _EXPORT_NAMES:
            fn = exports.get(name)
            if fn is None:
                raise RuntimeError(f"quickjs.wasm is missing export {name!r}")
            self._exports[name] = fn  # type: ignore[assignment]

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
    "qjs_get_global_object",
    "qjs_get_prop",
    "qjs_set_prop",
    "qjs_malloc",
    "qjs_free",
)


__all__ = ["Bridge"]
