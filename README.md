# quickjs-wasm

Sandboxed JavaScript execution for Python, hosted via a WASI build of QuickJS.

Status: **pre-v0.1**. The spec is frozen; the implementation is being written
against it. See `spec/implementation.md` for the authoritative design and
`tests/test_smoke.py` for the v0.1 acceptance criteria.

## Layers

1. `quickjs.wasm` — QuickJS (via [`quickjs-ng`](https://github.com/quickjs-ng/quickjs))
   compiled with WASI-SDK plus a C shim (`wasm/shim.c`) that exposes
   QuickJS's API as wasm exports.
2. `quickjs_wasm._bridge` — `wasmtime-py` wiring. Denies all WASI
   capabilities by default (no FS, no network, no real clock, no stdio).
3. `quickjs_wasm` — the public Python API: `Runtime`, `Context`, `Handle`.

## Development

```bash
# One-time setup
git submodule update --init --recursive
./scripts/install-wasi-sdk.sh

# Build the wasm after changes to shim.c or the submodule
./wasm/build.sh

# Install the package in dev mode
pip install -e ".[dev]"

# Run tests / type check / lint
pytest
mypy quickjs_wasm
ruff check
```

See `CLAUDE.md` for commit discipline and implementation order.

## License

MIT. Bundles QuickJS (also MIT). See `LICENSE`.
