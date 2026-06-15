//! Wire encode: `Value` -> bytes, canonical by construction per
//! `docs/adr/0002-wire-codec.md`. Because there is exactly one canonical
//! encoding per value, `encode(decode(bytes)) == bytes` for canonical input,
//! and the conformance suite asserts `encode(value) == hex` for every OK
//! vector (the bidirectional rule).
//!
//! Encoding a *non-canonical* value is a caller bug, not attacker input;
//! we still normalize where cheap (NaN -> canonical) so a stray bit pattern
//! can't produce non-canonical bytes.

use crate::value::{is_nan_bits, CANONICAL_NAN_BITS};
use crate::{limits, Reason, Value};

/// Encode a value to its canonical wire bytes.
///
/// Fallible and depth-counted (spec rule 5: "never blindly recursive"),
/// symmetric with the decoder. A `Value` nested deeper than the depth cap is
/// rejected with `DepthExceeded` rather than overflowing the host stack — the
/// marshaller should never construct one, but the reference codec must not
/// depend on its caller for memory safety, since the Python/TS encoders
/// mirror it.
pub fn encode_value(v: &Value) -> Result<Vec<u8>, Reason> {
    let mut out = Vec::new();
    encode_into(v, &mut out, 0)?;
    Ok(out)
}

fn encode_into(v: &Value, out: &mut Vec<u8>, depth: usize) -> Result<(), Reason> {
    if depth > limits::MAX_DEPTH {
        return Err(Reason::DepthExceeded);
    }
    match v {
        Value::Null => out.push(0x00),
        Value::Undefined => out.push(0x01),
        Value::Bool(false) => out.push(0x02),
        Value::Bool(true) => out.push(0x03),
        Value::Number(bits) => {
            out.push(0x04);
            // Normalize any NaN to the canonical pattern so we never emit a
            // non-canonical NaN even if handed stray bits.
            let bits = if is_nan_bits(*bits) { CANONICAL_NAN_BITS } else { *bits };
            out.extend_from_slice(&bits.to_le_bytes());
        }
        Value::BigInt(s) => {
            out.push(0x05);
            write_len_prefixed(s.as_bytes(), out);
        }
        Value::String(s) => {
            out.push(0x06);
            write_len_prefixed(s.as_bytes(), out);
        }
        Value::Bytes(b) => {
            out.push(0x07);
            write_len_prefixed(b, out);
        }
        Value::Array(items) => {
            out.push(0x08);
            out.extend_from_slice(&(items.len() as u32).to_le_bytes());
            for it in items {
                encode_into(it, out, depth + 1)?;
            }
        }
        Value::Object(pairs) => {
            out.push(0x09);
            out.extend_from_slice(&(pairs.len() as u32).to_le_bytes());
            for (k, val) in pairs {
                // Key is a String body (len + utf8), no tag.
                write_len_prefixed(k.as_bytes(), out);
                encode_into(val, out, depth + 1)?;
            }
        }
        Value::Handle(h) => {
            out.push(0x0A);
            out.extend_from_slice(&h.context_id.to_le_bytes());
            out.extend_from_slice(&h.handle_id.to_le_bytes());
            out.extend_from_slice(&h.generation.to_le_bytes());
        }
        Value::Error(e) => {
            out.push(0x0B);
            write_len_prefixed(e.name.as_bytes(), out);
            write_len_prefixed(e.message.as_bytes(), out);
            match &e.stack {
                Some(s) => write_len_prefixed(s.as_bytes(), out),
                None => out.extend_from_slice(&0u32.to_le_bytes()),
            }
        }
    }
    Ok(())
}

fn write_len_prefixed(bytes: &[u8], out: &mut Vec<u8>) {
    out.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
    out.extend_from_slice(bytes);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::CANONICAL_NAN_BITS;
    use crate::{decode_value, Value};

    /// Encoding a non-canonical NaN bit pattern still emits canonical bytes
    /// (which then decode back without a NonCanonicalNan reject).
    #[test]
    fn encode_normalizes_noncanonical_nan() {
        let signaling_nan = Value::Number(0x7FF0_0000_0000_0001);
        let bytes = encode_value(&signaling_nan).unwrap();
        // bytes 1..9 are the f64 LE; should be the canonical pattern.
        let mut want = vec![0x04];
        want.extend_from_slice(&CANONICAL_NAN_BITS.to_le_bytes());
        assert_eq!(bytes, want);
        assert_eq!(decode_value(&bytes).unwrap(), Value::Number(CANONICAL_NAN_BITS));
    }

    /// decode(encode(v)) == v across a representative deep value.
    #[test]
    fn round_trip_nested() {
        let v = Value::Object(vec![
            ("a".into(), Value::Array(vec![Value::Null, Value::Bool(true), Value::number(1.5)])),
            ("b".into(), Value::Object(vec![("c".into(), Value::String("x".into()))])),
            ("d".into(), Value::BigInt("-12345678901234567890".into())),
            ("e".into(), Value::Bytes(vec![0x00, 0xFF, 0x1A])),
        ]);
        assert_eq!(decode_value(&encode_value(&v).unwrap()).unwrap(), v);
    }

    /// Signed zero is preserved distinctly through a round trip.
    #[test]
    fn signed_zero_distinct() {
        let pos = Value::number(0.0);
        let neg = Value::number(-0.0);
        assert_ne!(pos, neg);
        assert_eq!(decode_value(&encode_value(&pos).unwrap()).unwrap(), pos);
        assert_eq!(decode_value(&encode_value(&neg).unwrap()).unwrap(), neg);
    }

    /// Encoder is depth-bounded (spec rule 5), symmetric with the decoder: a
    /// Value nested past the cap is rejected, not a stack overflow. Build a
    /// tree just over MAX_DEPTH and confirm DepthExceeded.
    #[test]
    fn encode_rejects_over_depth() {
        use crate::limits::MAX_DEPTH;
        // depth at cap encodes fine; cap+1 rejects.
        let mut at_cap = Value::Null;
        for _ in 0..MAX_DEPTH {
            at_cap = Value::Array(vec![at_cap]);
        }
        assert!(encode_value(&at_cap).is_ok());

        let over = Value::Array(vec![at_cap]); // one deeper than the ok case
        assert_eq!(encode_value(&over), Err(Reason::DepthExceeded));
    }
}
