"""WASI toolchain selection for the additional diff guest."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def build_guest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_guest.py"
    spec = importlib.util.spec_from_file_location("build_guest_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_ROOT", tmp_path)
    for name in ("WASI_SDK", "CC_wasm32_wasip1", "AR_wasm32_wasip1"):
        monkeypatch.delenv(name, raising=False)
    return module


def _sdk(path: Path) -> tuple[Path, Path]:
    suffix = ".exe" if sys.platform == "win32" else ""
    path.mkdir(parents=True)
    compiler, archiver = path / f"clang{suffix}", path / f"llvm-ar{suffix}"
    compiler.touch()
    archiver.touch()
    return compiler, archiver


def test_explicit_compilers_need_no_cached_sdk(
    build_guest: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CC_wasm32_wasip1", "custom-clang")
    monkeypatch.setenv("AR_wasm32_wasip1", "custom-ar")
    env = build_guest._diff_build_environment()
    assert env["CC_wasm32_wasip1"] == "custom-clang"
    assert env["AR_wasm32_wasip1"] == "custom-ar"


def test_explicit_sdk(build_guest: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = build_guest._ROOT / "sdk"
    compiler, archiver = _sdk(sdk / "bin")
    monkeypatch.setenv("WASI_SDK", str(sdk))
    env = build_guest._diff_build_environment()
    assert env["CC_wasm32_wasip1"] == str(compiler)
    assert env["AR_wasm32_wasip1"] == str(archiver)


def test_reuses_quickjs_sdk(build_guest: ModuleType) -> None:
    sdk = (
        build_guest._ROOT
        / "crates/quickjs-wasm/target/wasm32-wasip1/release/build/rquickjs-sys-test/out/wasi-sdk"
    )
    compiler, archiver = _sdk(sdk / "bin")
    env = build_guest._diff_build_environment()
    assert env["CC_wasm32_wasip1"] == str(compiler)
    assert env["AR_wasm32_wasip1"] == str(archiver)


def test_missing_sdk_explains_configuration(build_guest: ModuleType) -> None:
    with pytest.raises(SystemExit, match="WASI_SDK"):
        build_guest._diff_build_environment()
