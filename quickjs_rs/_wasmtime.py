"""Process-shared wasmtime engine and compiled-artifact cache."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass

import wasmtime

_SHARED_ENGINE: wasmtime.Engine | None = None
_SHARED_ENGINE_LOCK = threading.Lock()


@dataclass(frozen=True)
class SharedWasmArtifact:
    module: wasmtime.Module
    linker: wasmtime.Linker
    build_id: bytes


_SHARED_ARTIFACTS: dict[Hashable, SharedWasmArtifact] = {}
_SHARED_ARTIFACTS_LOCK = threading.Lock()


def shared_wasmtime_engine() -> wasmtime.Engine:
    global _SHARED_ENGINE
    if _SHARED_ENGINE is None:
        with _SHARED_ENGINE_LOCK:
            if _SHARED_ENGINE is None:
                _SHARED_ENGINE = wasmtime.Engine()
    return _SHARED_ENGINE


def shared_wasm_artifact(
    key: Hashable,
    read_wasm: Callable[[], bytes],
    build_linker: Callable[[wasmtime.Engine], wasmtime.Linker] | None = None,
) -> SharedWasmArtifact:
    """Compile and cache a WASM artifact against the process-shared engine."""
    artifact = _SHARED_ARTIFACTS.get(key)
    if artifact is not None:
        return artifact

    with _SHARED_ARTIFACTS_LOCK:
        artifact = _SHARED_ARTIFACTS.get(key)
        if artifact is None:
            engine = shared_wasmtime_engine()
            wasm_bytes = read_wasm()
            module = wasmtime.Module(engine, wasm_bytes)
            linker = (
                build_linker(engine)
                if build_linker is not None
                else default_wasi_linker(engine)
            )
            artifact = SharedWasmArtifact(
                module=module,
                linker=linker,
                build_id=hashlib.sha256(wasm_bytes).digest(),
            )
            _SHARED_ARTIFACTS[key] = artifact
    return artifact


def instantiate_wasm_artifact(
    artifact: SharedWasmArtifact,
    store: wasmtime.Store,
) -> wasmtime.Instance:
    return artifact.linker.instantiate(store, artifact.module)


def default_wasi_linker(engine: wasmtime.Engine) -> wasmtime.Linker:
    linker = wasmtime.Linker(engine)
    linker.define_wasi()
    return linker
