"""Hatchling build hook: build + bundle the guest wasm before packaging.

Runs `scripts/build_guest.py` (cargo build → copy to quickjs_rs/_guest.wasm) at
both wheel-build and sdist time, and registers the produced wasm as a force-
included build artifact so it lands in the wheel. Build-fresh: the wasm is
reproduced from source on every build; nothing binary is committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_ROOT = Path(__file__).resolve().parent
_BUNDLED = "quickjs_rs/_guest.wasm"


class GuestWasmBuildHook(BuildHookInterface):
    PLUGIN_NAME = "guest-wasm"

    def initialize(self, version: str, build_data: dict) -> None:
        # Import lazily so a `scripts/` path issue surfaces here, not at import.
        sys.path.insert(0, str(_ROOT / "scripts"))
        import build_guest  # noqa: E402

        build_guest.build()
        # Ensure the produced artifact is included in the build (wheel + sdist).
        build_data.setdefault("force_include", {})[str(_ROOT / _BUNDLED)] = _BUNDLED
        build_data.setdefault("artifacts", []).append(_BUNDLED)
