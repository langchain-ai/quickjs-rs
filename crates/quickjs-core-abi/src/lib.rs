//! `quickjs-core-abi` — the shared ABI value model and wire codec for the
//! WASM execution plane.
//!
//! This crate is the **reference codec** for the wire format the spec
//! (`docs/adr/0002-wire-codec.md`) defines. It is pure host Rust — no
//! rquickjs, no wasm — so the guest, every host adapter, and the conformance
//! tests can share one definition of "what the bytes mean."
//!
//! **Scope: the full wire codec.** `encode_value`/`decode_value` for values,
//! `encode_envelope`/`decode_envelope` for request frames, and
//! `decode_response`/`encode_descriptor` for the `AbiResponse` frame — all
//! validated against every one of the 76 conformance vectors (value +
//! envelope + response) by `tests/conformance.rs`, nothing deferred.
//!
//! Built up in layers (see git history): types → value decode → value encode
//! → conformance runner → envelope/response framing + ABI version.

mod decode;
mod encode;
mod frame;
mod reason;
mod status;
mod value;

pub use decode::decode_value;
pub use encode::encode_value;
pub use frame::{
    decode_envelope, decode_response, encode_descriptor, encode_envelope,
    invalid_request_descriptor, DecodedResponse, Envelope,
};
pub use reason::Reason;
pub use status::{AbiResponse, OkShape, Status};
pub use value::{is_nan_bits, ErrorRecord, Handle, Value, CANONICAL_NAN_BITS};

/// The wire ABI version. Both the guest's `qrs_abi_version` export and every
/// host check this; a mismatch fails fast (`AbiMismatch`) — no best-effort
/// cross-version parsing (spec → Versioning rule).
pub const ABI_VERSION: u32 = 1;

/// Caps enforced by the codec, mirroring the spec's Limits table.
pub mod limits {
    /// Max nesting depth (matches the native `MAX_MARSHAL_DEPTH`).
    pub const MAX_DEPTH: usize = 128;
    /// Max total payload for a response.
    pub const MAX_RESPONSE_BYTES: usize = 32 * 1024 * 1024;
    /// Max total payload for a host-call argument.
    pub const MAX_HOST_CALL_ARG_BYTES: usize = 8 * 1024 * 1024;
}
