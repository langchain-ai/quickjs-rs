//! Wire decode: bytes -> `Value`, fail-closed per `docs/adr/0002-wire-codec.md`.
//!
//! Every length is bounds-checked with overflow-safe arithmetic; every
//! non-canonical form is rejected with a specific `Reason`; nesting is
//! depth-counted (never blindly recursive). The decoder treats all input as
//! untrusted — this is the host's security kernel.

use crate::value::{is_nan_bits, CANONICAL_NAN_BITS};
use crate::{limits, ErrorRecord, Handle, Reason, Value};

/// A bounds-checked, overflow-safe cursor over an untrusted byte slice.
/// Crate-visible so the envelope/response codec can decode an embedded value
/// payload through the same hardened primitives.
pub(crate) struct Cursor<'a> {
    buf: &'a [u8],
    pos: usize,
    /// Cap a single length-prefixed field is validated against (the total
    /// payload cap; a declared length above it is `SizeExceeded`).
    max_bytes: usize,
}

impl<'a> Cursor<'a> {
    pub(crate) fn new(buf: &'a [u8], max_bytes: usize) -> Self {
        Cursor { buf, pos: 0, max_bytes }
    }

    pub(crate) fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    /// Take exactly `n` bytes, or `Truncated` if fewer remain. Uses
    /// overflow-safe `checked_add` for `pos + n` — `LengthOverflow` on wrap.
    /// A short read here is always `Truncated`; the distinct
    /// `LengthExceedsBuffer` / `SizeExceeded` reasons belong only to
    /// length-prefixed reads (see `take_len_prefixed`), where a *declared*
    /// length is validated before any bytes are consumed.
    pub(crate) fn take(&mut self, n: usize) -> Result<&'a [u8], Reason> {
        let end = self.pos.checked_add(n).ok_or(Reason::LengthOverflow)?;
        if end > self.buf.len() {
            return Err(Reason::Truncated);
        }
        let s = &self.buf[self.pos..end];
        self.pos = end;
        Ok(s)
    }

    fn take_u8(&mut self) -> Result<u8, Reason> {
        Ok(self.take(1)?[0])
    }

    pub(crate) fn take_u32(&mut self) -> Result<u32, Reason> {
        let b = self.take(4)?;
        Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }

    pub(crate) fn take_u64(&mut self) -> Result<u64, Reason> {
        let b = self.take(8)?;
        Ok(u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]]))
    }

    /// Read a `u32` length and then that many bytes, treating the length as
    /// an untrusted size validated *before* any bytes are read:
    ///   - over the total-size cap  -> `SizeExceeded`
    ///   - over the remaining bytes -> `LengthExceedsBuffer`
    /// (so we never trust a length to size work/alloc).
    fn take_len_prefixed(&mut self) -> Result<&'a [u8], Reason> {
        let len = self.take_u32()? as usize;
        if len > self.max_bytes {
            return Err(Reason::SizeExceeded);
        }
        if len > self.remaining() {
            return Err(Reason::LengthExceedsBuffer);
        }
        self.take(len)
    }
}

/// Decode one top-level value from `buf`. Rejects trailing bytes.
pub fn decode_value(buf: &[u8]) -> Result<Value, Reason> {
    if buf.is_empty() {
        return Err(Reason::Truncated);
    }
    if buf.len() > limits::MAX_RESPONSE_BYTES {
        return Err(Reason::SizeExceeded);
    }
    let mut cur = Cursor::new(buf, limits::MAX_RESPONSE_BYTES);
    let v = decode_one(&mut cur, 0)?;
    if cur.remaining() != 0 {
        return Err(Reason::TrailingBytes);
    }
    Ok(v)
}

/// Decode one value from a cursor (depth 0), for embedded payloads
/// (envelope / response). The caller owns trailing-bytes policy.
pub(crate) fn decode_value_from(cur: &mut Cursor) -> Result<Value, Reason> {
    decode_one(cur, 0)
}

fn decode_one(cur: &mut Cursor, depth: usize) -> Result<Value, Reason> {
    if depth > limits::MAX_DEPTH {
        return Err(Reason::DepthExceeded);
    }
    let tag = cur.take_u8()?;
    match tag {
        0x00 => Ok(Value::Null),
        0x01 => Ok(Value::Undefined),
        0x02 => Ok(Value::Bool(false)),
        0x03 => Ok(Value::Bool(true)),
        0x04 => {
            let b = cur.take(8)?;
            let bits = u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]]);
            // Canonical-NaN rule: any NaN-class pattern that is not the
            // canonical quiet NaN is rejected.
            if is_nan_bits(bits) && bits != CANONICAL_NAN_BITS {
                return Err(Reason::NonCanonicalNan);
            }
            Ok(Value::Number(bits))
        }
        0x05 => {
            let bytes = cur.take_len_prefixed()?;
            let s = std::str::from_utf8(bytes).map_err(|_| Reason::InvalidUtf8)?;
            if !is_canonical_bigint(s) {
                return Err(Reason::NonCanonicalBigint);
            }
            Ok(Value::BigInt(s.to_owned()))
        }
        0x06 => {
            let bytes = cur.take_len_prefixed()?;
            let s = std::str::from_utf8(bytes).map_err(|_| Reason::InvalidUtf8)?;
            Ok(Value::String(s.to_owned()))
        }
        0x07 => {
            let bytes = cur.take_len_prefixed()?;
            Ok(Value::Bytes(bytes.to_vec()))
        }
        0x08 => {
            let count = cur.take_u32()? as usize;
            // A count can't exceed remaining bytes (each element is >=1 byte).
            if count > cur.remaining() {
                return Err(Reason::LengthExceedsBuffer);
            }
            // Do NOT pre-reserve from `count`: it is untrusted, and within the
            // size cap a 1-byte-per-element count can still be tens of millions
            // (gigabytes of `Value` slots) — the same "never size an alloc from
            // a length" rule `take_len_prefixed` honors. Let the Vec grow against
            // what is actually decoded; each push consumed >=1 real byte.
            let mut items = Vec::new();
            for _ in 0..count {
                items.push(decode_one(cur, depth + 1)?);
            }
            Ok(Value::Array(items))
        }
        0x09 => {
            let count = cur.take_u32()? as usize;
            if count > cur.remaining() {
                return Err(Reason::LengthExceedsBuffer);
            }
            // Same: no pre-reservation from the untrusted count.
            // Duplicate detection via a HashSet — O(1) per key, not the O(n^2)
            // of a linear scan over `pairs` (a within-cap object can hold many
            // keys; the quadratic scan was a CPU amplification sibling to the
            // allocation one). Set size is bounded by total key bytes ~= input.
            let mut pairs: Vec<(String, Value)> = Vec::new();
            let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
            for _ in 0..count {
                // Key is a String BODY (len + utf8), no value tag.
                let kb = cur.take_len_prefixed()?;
                let key = std::str::from_utf8(kb).map_err(|_| Reason::InvalidUtf8)?.to_owned();
                if !seen.insert(key.clone()) {
                    return Err(Reason::DuplicateObjectKey);
                }
                let val = decode_one(cur, depth + 1)?;
                pairs.push((key, val));
            }
            Ok(Value::Object(pairs))
        }
        0x0A => {
            let b = cur.take(12)?;
            Ok(Value::Handle(Handle {
                context_id: u32::from_le_bytes([b[0], b[1], b[2], b[3]]),
                handle_id: u32::from_le_bytes([b[4], b[5], b[6], b[7]]),
                generation: u32::from_le_bytes([b[8], b[9], b[10], b[11]]),
            }))
        }
        0x0B => {
            let name = decode_str_field(cur)?;
            let message = decode_str_field(cur)?;
            let stack_bytes = cur.take_len_prefixed()?;
            let stack = if stack_bytes.is_empty() {
                None
            } else {
                Some(std::str::from_utf8(stack_bytes).map_err(|_| Reason::InvalidUtf8)?.to_owned())
            };
            Ok(Value::Error(ErrorRecord { name, message, stack }))
        }
        _ => Err(Reason::UnknownValueTag),
    }
}

fn decode_str_field(cur: &mut Cursor) -> Result<String, Reason> {
    let b = cur.take_len_prefixed()?;
    Ok(std::str::from_utf8(b).map_err(|_| Reason::InvalidUtf8)?.to_owned())
}

/// Canonical decimal: optional leading `-`, then digits with no leading zero
/// (except "0" alone), no "+", no "-0", no empty, no non-digits.
fn is_canonical_bigint(s: &str) -> bool {
    let digits = s.strip_prefix('-').unwrap_or(s);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return false;
    }
    // No leading zeros, except the single literal "0".
    if digits.len() > 1 && digits.starts_with('0') {
        return false;
    }
    // Reject "-0" (negative zero) and a bare "-".
    if s.starts_with('-') && digits == "0" {
        return false;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Regression for the allocation-amplification finding: a within-cap
    /// buffer that declares a huge element/pair count must reject promptly
    /// without pre-reserving gigabytes. We build a 1 MiB buffer whose array
    /// count is ~1M but which contains no elements after the count; it must
    /// fail fast (count > remaining) and never allocate from `count`.
    #[test]
    fn array_count_does_not_amplify_allocation() {
        // Array tag + count = 0x00100000 (1,048,576), then nothing.
        let mut buf = vec![0x08];
        buf.extend_from_slice(&1_048_576u32.to_le_bytes());
        // remaining after count = 0, count = ~1M -> LengthExceedsBuffer, no alloc.
        assert_eq!(decode_value(&buf), Err(Reason::LengthExceedsBuffer));
    }

    /// A count that passes the `> remaining` guard (count <= remaining) but
    /// whose elements run short still rejects via per-element decode, again
    /// without reserving `count` slots. Array count=3, one real element.
    #[test]
    fn array_short_elements_reject_without_amplification() {
        let buf = [0x08, 0x03, 0x00, 0x00, 0x00, 0x00]; // count 3, one Null
        // count(3) > remaining(1) -> LengthExceedsBuffer before any decode.
        assert_eq!(decode_value(&buf), Err(Reason::LengthExceedsBuffer));
    }

    /// Duplicate-key detection still works after switching the linear scan to
    /// a HashSet (the O(n^2) -> O(n) fix). Object count=2, key "a" twice.
    #[test]
    fn object_duplicate_key_still_rejected() {
        // 09 | count 2 | (len1 "a") 02 | (len1 "a") 03
        let buf = [
            0x09, 0x02, 0x00, 0x00, 0x00, // Object, count 2
            0x01, 0x00, 0x00, 0x00, b'a', 0x02, // "a": false
            0x01, 0x00, 0x00, 0x00, b'a', 0x03, // "a": true (dup)
        ];
        assert_eq!(decode_value(&buf), Err(Reason::DuplicateObjectKey));
    }
}
