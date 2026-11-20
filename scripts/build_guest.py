#!/usr/bin/env python3
"""Build the WASM guests and bundle them into the package.

Runs `cargo build --release --target wasm32-wasip1` in each guest crate, then
copies the artifacts into `quickjs_rs/` as package data. The wheel build hook
calls this so the wheel ships the wasm; developers run it directly for the
in-tree edit-build-test loop:

    python scripts/build_guest.py

Build-fresh: bundled wasm files are build artifacts (gitignored), reproduced
from source on every build, and never committed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TARGET = "wasm32-wasip1"
_GUESTS = (
    (
        "quickjs guest",
        _ROOT / "crates" / "quickjs-wasm",
        "quickjs_wasm.wasm",
        _ROOT / "quickjs_rs" / "_guest.wasm",
    ),
    (
        "transform guest",
        _ROOT / "crates" / "quickjs-wasm-transform",
        "quickjs_wasm_transform.wasm",
        _ROOT / "quickjs_rs" / "_transform.wasm",
    ),
    (
        "snapshot diff guest",
        _ROOT / "crates" / "snapshot-diff-wasm",
        "snapshot_diff_wasm.wasm",
        _ROOT / "quickjs_rs" / "_diff.wasm",
    ),
)


def _check_toolchain() -> None:
    if shutil.which("cargo") is None:
        sys.exit(
            "error: `cargo` not found on PATH. Install the Rust toolchain "
            "(https://rustup.rs) to build the guest wasm."
        )
    # Confirm the wasm target is installed (rustup target add wasm32-wasip1).
    try:
        out = subprocess.run(
            ["rustup", "target", "list", "--installed"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if _TARGET not in out.split():
            sys.exit(
                f"error: the `{_TARGET}` Rust target is not installed. Run:\n"
                f"    rustup target add {_TARGET}"
            )
    except (FileNotFoundError, subprocess.CalledProcessError):
        # No rustup (e.g. distro rust). cargo will fail clearly if the target
        # is missing; don't hard-block here.
        pass


def _diff_build_environment() -> dict[str, str]:
    """Reuse QuickJS's downloaded WASI SDK unless compilers are configured."""
    env = os.environ.copy()
    if env.get("CC_wasm32_wasip1") and env.get("AR_wasm32_wasip1"):
        return env
    suffix = ".exe" if sys.platform == "win32" else ""
    if env.get("WASI_SDK"):
        sdk = Path(env["WASI_SDK"])
    else:
        build_dir = _ROOT / "crates" / "quickjs-wasm" / "target" / _TARGET / "release" / "build"
        candidates = sorted(build_dir.glob(f"rquickjs-sys-*/out/wasi-sdk/bin/clang{suffix}"))
        if not candidates:
            sys.exit("error: set WASI_SDK or CC_wasm32_wasip1/AR_wasm32_wasip1 for the diff guest")
        sdk = candidates[0].parent.parent
    compiler = sdk / "bin" / f"clang{suffix}"
    archiver = sdk / "bin" / f"llvm-ar{suffix}"
    if not compiler.is_file() or not archiver.is_file():
        sys.exit(f"error: WASI SDK is missing clang/llvm-ar: {sdk}")
    env.setdefault("CC_wasm32_wasip1", str(compiler))
    env.setdefault("AR_wasm32_wasip1", str(archiver))
    return env


def build() -> list[Path]:
    """Build and bundle the WASM guests."""
    _check_toolchain()
    bundled: list[Path] = []
    for label, crate_dir, artifact_name, bundle_dest in _GUESTS:
        artifact = crate_dir / "target" / _TARGET / "release" / artifact_name
        print(f"building {label} ({_TARGET}) in {crate_dir} ...", file=sys.stderr)
        subprocess.run(
            ["cargo", "build", "--release", "--target", _TARGET],
            cwd=crate_dir,
            check=True,
            env=_diff_build_environment() if artifact_name == "snapshot_diff_wasm.wasm" else None,
        )
        if not artifact.exists():
            sys.exit(f"error: expected artifact not produced at {artifact}")
        bundle_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, bundle_dest)
        size_mb = bundle_dest.stat().st_size / 1_048_576
        print(f"bundled -> {bundle_dest} ({size_mb:.2f} MB)", file=sys.stderr)
        bundled.append(bundle_dest)
    return bundled


if __name__ == "__main__":
    build()
