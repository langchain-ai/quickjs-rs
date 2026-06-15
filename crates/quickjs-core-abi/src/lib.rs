//! `quickjs-core-abi` — the shared ABI value model and wire codec for the
//! WASM execution plane.
//!
//! This crate is the **reference codec**: the spec
//! (`docs/adr/0002-wire-codec.md`) made executable, validated against the
//! conformance suite (`conformance/abi/codec_vectors.jsonl`). It is pure host
//! Rust — no rquickjs, no wasm — so the guest, every host adapter, and the
//! conformance tests can share one definition of "what the bytes mean."
//!
//! Built up in layers (see git history): types → decode → encode →
//! debug-JSON → conformance runner.

// Layers land in subsequent commits.
