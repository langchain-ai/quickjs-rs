# Threat Model: quickjs-rs

The threat model now lives with the rest of the documentation:

- Site: https://langchain-ai.github.io/quickjs-rs/reference/threat-model
- Source: [`docs/content/reference/threat-model.mdx`](../docs/content/reference/threat-model.mdx)

It covers the WebAssembly-based engine (QuickJS compiled to `wasm32-wasip1`, driven by wasmtime). The previous
model in this file described the PyO3 extension, which no longer exists; it is in this file's git history.
