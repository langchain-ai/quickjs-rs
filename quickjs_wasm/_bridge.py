"""wasmtime wiring. See spec/implementation.md §7, §9.

Internal: loads quickjs.wasm, stubs WASI imports (all denied by default
per §9), implements host_call and host_interrupt, and exposes a thin
Python-side wrapper over the qjs_* shim exports.
"""

from __future__ import annotations
