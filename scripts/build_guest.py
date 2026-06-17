#!/usr/bin/env python3
"""Build the guest wasm and bundle it into the package.

Runs `cargo build --release --target wasm32-wasip1` in `crates/quickjs-wasm/`, then copies the
artifact to `quickjs_rs/_guest.wasm` (package data). The wheel build hook calls
this so the wheel ships the wasm; developers run it directly for the in-tree
edit→build→test loop:

    python scripts/build_guest.py

Build-fresh: the bundled `quickjs_rs/_guest.wasm` is a build artifact (gitignored),
reproduced from source on every build — never committed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_GUEST_DIR = _ROOT / "crates" / "quickjs-wasm"
_TARGET = "wasm32-wasip1"
_ARTIFACT = _GUEST_DIR / "target" / _TARGET / "release" / "quickjs_wasm.wasm"
_BUNDLE_DEST = _ROOT / "quickjs_rs" / "_guest.wasm"


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


def build() -> Path:
    """Build the guest wasm and copy it into the package. Returns the dest."""
    _check_toolchain()
    print(f"building guest wasm ({_TARGET}) in {_GUEST_DIR} ...", file=sys.stderr)
    subprocess.run(
        ["cargo", "build", "--release", "--target", _TARGET],
        cwd=_GUEST_DIR, check=True,
    )
    if not _ARTIFACT.exists():
        sys.exit(f"error: expected artifact not produced at {_ARTIFACT}")
    _BUNDLE_DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ARTIFACT, _BUNDLE_DEST)
    size_mb = _BUNDLE_DEST.stat().st_size / 1_048_576
    print(f"bundled → {_BUNDLE_DEST} ({size_mb:.2f} MB)", file=sys.stderr)
    return _BUNDLE_DEST


if __name__ == "__main__":
    build()
