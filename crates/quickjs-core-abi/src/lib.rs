//! `quickjs-core-abi` — the shared ABI value model and wire codec for the
//! WASM execution plane.
//!
//! This crate is the **reference codec** for the wire format the spec
//! (`docs/adr/0002-wire-codec.md`) defines. It is pure host Rust — no
//! rquickjs, no wasm — so the guest, every host adapter, and the conformance
//! tests can share one definition of "what the bytes mean."
//!
//! **V1 scope: the value-level codec.** `encode_value`/`decode_value` cover
//! the 67 value-kind conformance vectors, validated by
//! `tests/conformance.rs`. The envelope and `AbiResponse` codec layers are
//! not yet implemented — the 9 envelope/response conformance vectors are
//! counted-and-deferred by the runner, not exercised. Some types here ship
//! ahead of their codec layer for that reason and currently have no producer:
//! `Status::from_u32`, `OkShape`, and the envelope/response reason codes
//! (`ReservedFlagSet`, `UnknownStatus`, `UnknownResponseTag`). They land with
//! the envelope/response layers.
//!
//! Built up in layers (see git history): types → value decode → value encode
//! → conformance runner → (next) envelope/response codec.

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
