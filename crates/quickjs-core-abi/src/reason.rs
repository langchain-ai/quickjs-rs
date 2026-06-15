//! Decode-failure reasons — the rejection-reason taxonomy from
//! `docs/adr/0002-wire-codec.md`.
//!
//! A reason is a **decoder-side classification** of a local decode failure,
//! raised by whichever codec is reading bytes (usually the host decoding
//! guest output — a local raise, never a wire value). When the *guest* is the
//! rejecting decoder, the reason also rides `AbiResponse.tag` under the
//! `InvalidRequest` status. The conformance suite asserts the reason so three
//! decoders can't reject for three different reasons and falsely agree.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u32)]
pub enum Reason {
    UnknownValueTag,
    UnknownStatus,
    UnknownResponseTag,
    NonCanonicalNan,
    NonCanonicalBigint,
    InvalidUtf8,
    LengthExceedsBuffer,
    LengthOverflow,
    DepthExceeded,
    SizeExceeded,
    TrailingBytes,
    Truncated,
    ReservedFlagSet,
    DuplicateObjectKey,
}

impl Reason {
    /// Stable snake_case name — matches the `reject` reason strings in
    /// conformance/abi/codec_vectors.jsonl, so the conformance runner can
    /// compare directly.
    pub fn as_str(self) -> &'static str {
        use Reason::*;
        match self {
            UnknownValueTag => "unknown_value_tag",
            UnknownStatus => "unknown_status",
            UnknownResponseTag => "unknown_response_tag",
            NonCanonicalNan => "non_canonical_nan",
            NonCanonicalBigint => "non_canonical_bigint",
            InvalidUtf8 => "invalid_utf8",
            LengthExceedsBuffer => "length_exceeds_buffer",
            LengthOverflow => "length_overflow",
            DepthExceeded => "depth_exceeded",
            SizeExceeded => "size_exceeded",
            TrailingBytes => "trailing_bytes",
            Truncated => "truncated",
            ReservedFlagSet => "reserved_flag_set",
            DuplicateObjectKey => "duplicate_object_key",
        }
    }
}
