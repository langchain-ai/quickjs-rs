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
            capture_output=True, text=True, check=True,
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


def build() -> list[Path]:
    """Build both wasm guests and copy them into the package."""
    _check_toolchain()
    bundled: list[Path] = []
    for label, crate_dir, artifact_name, bundle_dest in _GUESTS:
        artifact = crate_dir / "target" / _TARGET / "release" / artifact_name
        print(f"building {label} ({_TARGET}) in {crate_dir} ...", file=sys.stderr)
        subprocess.run(
            ["cargo", "build", "--release", "--target", _TARGET],
            cwd=crate_dir,
            check=True,
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
