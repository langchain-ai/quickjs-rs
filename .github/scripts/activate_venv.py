"""Publish `.venv` to `$GITHUB_ENV` + `$GITHUB_PATH` for subsequent
steps to find.

Used by test.yml and benchmarks.yml. `maturin develop` refuses to
install into a bare Python (no VIRTUAL_ENV / CONDA_PREFIX / .venv
marker), so we create `.venv` first and export it here.

Why a Python script instead of an inline bash one-liner: on Windows,
GitHub Actions' default bash is Git Bash, and $PWD / $GITHUB_WORKSPACE
inside it give Git-Bash virtual paths (/d/a/...) that subsequent
PowerShell or cmd steps — or Windows Python itself — can't resolve.
Python's pathlib handles the host-native form on every platform.

Run from the repo root; expects `.venv/` to exist already.
"""

from __future__ import annotations

import os
import pathlib
import sys


def main() -> int:
    venv = pathlib.Path(".venv").resolve()
    if not venv.is_dir():
        print(f"venv not found at {venv}", file=sys.stderr)
        return 1

    # Windows puts executables in Scripts/, everything else in bin/.
    bin_dir = venv / ("Scripts" if sys.platform == "win32" else "bin")
    if not bin_dir.is_dir():
        print(f"venv bin dir not found at {bin_dir}", file=sys.stderr)
        return 1

    github_env = os.environ.get("GITHUB_ENV")
    github_path = os.environ.get("GITHUB_PATH")
    if not github_env or not github_path:
        print("GITHUB_ENV / GITHUB_PATH not set", file=sys.stderr)
        return 1

    with open(github_env, "a", encoding="utf-8") as f:
        f.write(f"VIRTUAL_ENV={venv}\n")
    with open(github_path, "a", encoding="utf-8") as f:
        f.write(f"{bin_dir}\n")

    print(f"Activated venv: {venv}")
    print(f"Added to PATH: {bin_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
