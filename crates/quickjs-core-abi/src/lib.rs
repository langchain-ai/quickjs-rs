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

mod decode;
mod encode;
mod reason;
mod status;
mod value;

pub use decode::decode_value;
pub use encode::encode_value;
pub use reason::Reason;
pub use status::{AbiResponse, OkShape, Status};
pub use value::{is_nan_bits, ErrorRecord, Handle, Value, CANONICAL_NAN_BITS};

/// Caps enforced by the codec, mirroring the spec's Limits table.
pub mod limits {
    /// Max nesting depth (matches the native `MAX_MARSHAL_DEPTH`).
    pub const MAX_DEPTH: usize = 128;
    /// Max total payload for a response.
    pub const MAX_RESPONSE_BYTES: usize = 32 * 1024 * 1024;
    /// Max total payload for a host-call argument.
    pub const MAX_HOST_CALL_ARG_BYTES: usize = 8 * 1024 * 1024;
}
