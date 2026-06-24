"""Process-shared OXC transform artifact adapter."""

from __future__ import annotations

import hashlib
import threading
from importlib.resources import files
from typing import Any, cast

import wasmtime

from quickjs_rs._wasmtime import (
    SharedWasmArtifact,
    instantiate_wasm_artifact,
    shared_wasm_artifact,
    shared_wasmtime_engine,
)

_BUNDLED_TRANSFORM_WASM = "_transform.wasm"

STATUS_OK = 0
STATUS_UNCHANGED = 1
STATUS_BAD_INPUT = 2
STATUS_PARSE_ERROR = 3
STATUS_TRANSFORM_ERROR = 4
STATUS_PANIC = 5

FLAG_SOURCE_TS = 1 << 0
FLAG_SOURCE_TSX = 1 << 1
FLAG_STRIP_TYPESCRIPT = 1 << 8
FLAG_TOP_LEVEL_CONST_TO_VAR = 1 << 9
FLAG_TS_EXTENSION_IMPORT_TO_DYNAMIC_IMPORT = 1 << 10

_TS_EXTENSIONS = (".ts", ".mts", ".cts")
_TSX_EXTENSIONS = (".tsx",)


class TransformError(RuntimeError):
    """Source transform failed before QuickJS saw the module."""


def needs_transform(name: str) -> bool:
    return module_transform_flags(name) != 0


def transform_module_source(
    name: str,
    source: str,
    *,
    flags: int | None = None,
) -> str:
    """Transform one source using a temporary isolated transform instance.

    If `flags` is zero, the host derives default module transform policy from
    `name`. The transform WASM never infers source kind from a path.
    """
    transformer = SourceTransformer()
    try:
        return transformer.transform(name, source, flags=flags)
    finally:
        transformer.close()


def module_transform_flags(name: str) -> int:
    """Default host-side transform policy for module loading."""
    if name.endswith(_TSX_EXTENSIONS):
        return FLAG_SOURCE_TSX | FLAG_STRIP_TYPESCRIPT
    if name.endswith(_TS_EXTENSIONS):
        return FLAG_SOURCE_TS | FLAG_STRIP_TYPESCRIPT
    return 0


def _read_transform_wasm() -> bytes:
    res = files("quickjs_rs") / _BUNDLED_TRANSFORM_WASM
    if not res.is_file():
        raise TransformError(
            f"transform wasm not found (quickjs_rs/{_BUNDLED_TRANSFORM_WASM}). "
            "Build it with `python scripts/build_guest.py`."
        )
    return res.read_bytes()


def _transform_artifact() -> SharedWasmArtifact:
    return shared_wasm_artifact("transform", _read_transform_wasm)


class SourceTransformer:
    """Per-owner transform state.

    The compiled WASM artifact is process-shared, but each SourceTransformer
    owns its own Store/Instance and cache. A QuickJS instance gets one of these,
    so transform execution state follows the same trust boundary as QuickJS.
    """

    def __init__(self) -> None:
        self._artifact: SharedWasmArtifact | None = None
        self._instance: _TransformInstance | None = None
        self._lock = threading.RLock()
        self._cache: dict[tuple[bytes, str, bytes, int], str] = {}
        self._closed = False

    def transform(
        self,
        name: str,
        source: str,
        *,
        flags: int | None = None,
    ) -> str:
        if self._closed:
            raise TransformError("source transformer is closed")
        if flags is None:
            flags = module_transform_flags(name)
        else:
            flags = int(flags)
        if flags == 0:
            return source

        data = source.encode()
        artifact = self._transform_artifact()
        key = (artifact.build_id, name, hashlib.sha256(data).digest(), flags)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            inst = self._instance_for(artifact)
            try:
                transformed = inst.transform(name, data, flags=flags)
            except Exception:
                # A trap/panic can poison a store. Recreate from the compiled
                # module on the next call and let this call fail cleanly to the
                # module loader.
                inst.close()
                self._instance = None
                raise
            self._cache[key] = transformed
            return transformed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._instance is not None:
            self._instance.close()
            self._instance = None
        self._cache.clear()

    def _transform_artifact(self) -> SharedWasmArtifact:
        artifact = self._artifact
        if artifact is None:
            artifact = _transform_artifact()
            self._artifact = artifact
        return artifact

    def _instance_for(self, artifact: SharedWasmArtifact) -> _TransformInstance:
        inst = self._instance
        if inst is None:
            inst = _TransformInstance(artifact)
            self._instance = inst
        return inst


class _TransformInstance:
    def __init__(self, artifact: SharedWasmArtifact) -> None:
        self.store = wasmtime.Store(shared_wasmtime_engine())
        wasi = wasmtime.WasiConfig()
        wasi.inherit_stdout()
        wasi.inherit_stderr()
        self.store.set_wasi(wasi)
        self.inst = instantiate_wasm_artifact(artifact, self.store)
        exports = self.inst.exports(self.store)
        self.mem = cast(wasmtime.Memory, exports["memory"])
        self._exports = exports
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.store.close()

    def c(self, name: str, *args: Any) -> Any:
        fn = cast(wasmtime.Func, self._exports[name])
        return fn(self.store, *args)

    def transform(self, name: str, source: bytes, *, flags: int) -> str:
        name_bytes = name.encode()
        name_ptr = self.alloc_write(name_bytes)
        source_ptr = self.alloc_write(source)
        try:
            status = int(
                self.c(
                    "qjst_transform",
                    name_ptr,
                    len(name_bytes),
                    source_ptr,
                    len(source),
                    flags,
                )
            )
            if status == STATUS_UNCHANGED:
                return source.decode()
            if status == STATUS_OK:
                return self.take_result().decode()
            error = self.take_error().decode(errors="replace")
            raise TransformError(error or f"transform failed status={status}")
        finally:
            self.free(name_ptr, len(name_bytes))
            self.free(source_ptr, len(source))
            self.c("qjst_result_free")

    def _mem_size(self) -> int:
        return self.mem.data_len(self.store)

    def read(self, ptr: int, length: int) -> bytes:
        if ptr < 0 or length < 0 or ptr + length > self._mem_size():
            raise TransformError(f"out-of-range transform read ptr={ptr} len={length}")
        return bytes(self.mem.read(self.store, ptr, ptr + length))

    def alloc_write(self, data: bytes) -> int:
        ptr = int(self.c("qjst_alloc", len(data)))
        if ptr == 0 and data:
            raise TransformError("qjst_alloc returned null")
        if data:
            self.mem.write(self.store, data, ptr)
        return ptr

    def free(self, ptr: int, length: int) -> None:
        self.c("qjst_free", ptr, length)

    def take_result(self) -> bytes:
        length = int(self.c("qjst_result_len"))
        ptr = int(self.c("qjst_result_ptr"))
        return self.read(ptr, length) if length else b""

    def take_error(self) -> bytes:
        length = int(self.c("qjst_error_len"))
        ptr = int(self.c("qjst_error_ptr"))
        return self.read(ptr, length) if length else b""
