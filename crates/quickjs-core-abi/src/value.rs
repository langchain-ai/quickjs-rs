//! The ABI value model — the abstract values that cross the WASM/host
//! boundary, per `docs/adr/0002-wire-codec.md`.
//!
//! `derive(PartialEq)` gives the structural value-equality the conformance
//! suite compares with (equal variant, recursively equal contents). One
//! subtlety drives the `Number` representation:

/// A value crossing the boundary.
///
/// `Number` stores the **raw f64 bits** (`u64`), not an `f64`, on purpose:
/// the derived `PartialEq` must treat canonical NaN as equal to itself and
/// `-0.0` as *distinct* from `+0.0` — both of which native `f64` equality
/// gets wrong (NaN != NaN, and -0.0 == 0.0). Comparing bits gives exactly
/// the spec's semantics (canonical NaN is the only valid NaN; signed zero
/// preserved). Encoders/decoders enforce the canonical-NaN rule; this type
/// just holds the bits faithfully.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Undefined,
    Bool(bool),
    /// f64 as its raw IEEE-754 bit pattern (see type docs).
    Number(u64),
    /// Canonical decimal string (no leading zeros, `-` only for negatives,
    /// `0` not `-0`). String form avoids precision loss; the codec enforces
    /// canonicality.
    BigInt(String),
    String(String),
    Bytes(Vec<u8>),
    Array(Vec<Value>),
    /// Ordered key/value pairs — a list, not a map, so insertion order is
    /// preserved and duplicate keys are representable (the codec rejects
    /// duplicates on decode).
    Object(Vec<(String, Value)>),
    Handle(Handle),
    Error(ErrorRecord),
}

/// Opaque reference to a live guest object — the unforgeable, context-scoped,
/// reuse-safe identity triple. No raw pointer ever crosses. Validity
/// (live/disposed/cross-context) is a handle-table concern, not the codec's:
/// the codec reads 12 bytes regardless of the values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Handle {
    pub context_id: u32,
    pub handle_id: u32,
    pub generation: u32,
}

/// A JS error record. `stack` is absent (wire len 0) -> `None`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorRecord {
    pub name: String,
    pub message: String,
    pub stack: Option<String>,
}

impl Value {
    /// Convenience: build a `Number` from an `f64`, canonicalizing NaN to
    /// quiet NaN so the stored bits match the spec's single valid NaN.
    /// (`-0.0`/`+0.0` are preserved distinctly.)
    pub fn number(f: f64) -> Value {
        if f.is_nan() {
            Value::Number(CANONICAL_NAN_BITS)
        } else {
            Value::Number(f.to_bits())
        }
    }
}

/// The one canonical NaN bit pattern (quiet NaN). Any other NaN-class
/// pattern is rejected on decode; encoders emit only this.
pub const CANONICAL_NAN_BITS: u64 = 0x7FF8_0000_0000_0000;

/// `true` if `bits` is a NaN-class f64 (exponent all ones, nonzero mantissa).
pub fn is_nan_bits(bits: u64) -> bool {
    let exponent = (bits >> 52) & 0x7FF;
    let mantissa = bits & 0x000F_FFFF_FFFF_FFFF;
    exponent == 0x7FF && mantissa != 0
}
