"""Hatchling build hook: build + bundle WASM guests before packaging.

Runs `scripts/build_guest.py` at both wheel-build and sdist time, and registers
the produced wasm files as force-included build artifacts so they land in the
wheel. Build-fresh: wasm is reproduced from source on every build; nothing
binary is committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_ROOT = Path(__file__).resolve().parent
_BUNDLED = ("quickjs_rs/_guest.wasm", "quickjs_rs/_transform.wasm")


class GuestWasmBuildHook(BuildHookInterface):
    PLUGIN_NAME = "guest-wasm"

    def initialize(self, version: str, build_data: dict) -> None:
        # Import lazily so a `scripts/` path issue surfaces here, not at import.
        sys.path.insert(0, str(_ROOT / "scripts"))
        import build_guest  # noqa: E402

        build_guest.build()
        # Ensure the produced artifacts are included in the build (wheel + sdist).
        force_include = build_data.setdefault("force_include", {})
        artifacts = build_data.setdefault("artifacts", [])
        for bundled in _BUNDLED:
            force_include[str(_ROOT / bundled)] = bundled
            artifacts.append(bundled)
