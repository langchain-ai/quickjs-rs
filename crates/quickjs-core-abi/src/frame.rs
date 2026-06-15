//! Envelope (request) and AbiResponse (response) framing codec, per
//! `docs/adr/0002-wire-codec.md`. The value-level codec (`encode_value`/
//! `decode_value`) handles payloads; this layer wraps them in the
//! request/response frames.

use crate::decode::{decode_value_from, Cursor};
use crate::{encode::encode_value, limits, AbiResponse, OkShape, Reason, Status, Value};

/// A decoded request envelope.
#[derive(Debug, Clone, PartialEq)]
pub struct Envelope {
    pub abi_version: u32,
    pub request_id: u64,
    pub kind: u32,
    pub flags: u32,
    pub payload: Value,
}

/// A decoded response: the descriptor's status/tag plus the decoded payload.
#[derive(Debug, Clone, PartialEq)]
pub struct DecodedResponse {
    pub status: Status,
    pub tag: u32,
    pub payload: Value,
}

// --- Envelope ---------------------------------------------------------------

/// Decode a request envelope. Reserved (nonzero) `flags` is rejected
/// (`ReservedFlagSet`) — no flag bits are defined yet. Trailing bytes after
/// the payload are rejected.
pub fn decode_envelope(buf: &[u8]) -> Result<Envelope, Reason> {
    if buf.len() > limits::MAX_RESPONSE_BYTES {
        return Err(Reason::SizeExceeded);
    }
    let mut cur = Cursor::new(buf, limits::MAX_RESPONSE_BYTES);
    let abi_version = cur.take_u32()?;
    let request_id = cur.take_u64()?;
    let kind = cur.take_u32()?;
    let flags = cur.take_u32()?;
    if flags != 0 {
        return Err(Reason::ReservedFlagSet);
    }
    let payload = decode_value_from(&mut cur)?;
    if cur.remaining() != 0 {
        return Err(Reason::TrailingBytes);
    }
    Ok(Envelope { abi_version, request_id, kind, flags, payload })
}

/// Encode a request envelope (canonical).
pub fn encode_envelope(env: &Envelope) -> Result<Vec<u8>, Reason> {
    let mut out = Vec::new();
    out.extend_from_slice(&env.abi_version.to_le_bytes());
    out.extend_from_slice(&env.request_id.to_le_bytes());
    out.extend_from_slice(&env.kind.to_le_bytes());
    out.extend_from_slice(&env.flags.to_le_bytes());
    out.extend_from_slice(&encode_value(&env.payload)?);
    Ok(out)
}

// --- AbiResponse (descriptor + payload) -------------------------------------

/// Decode a response: a 16-byte descriptor followed by `len` payload bytes at
/// `ptr`. Here the payload is assumed to immediately follow the descriptor
/// (ptr = 16); a host reading guest memory would instead use `ptr` as an
/// offset into linear memory after validating it.
///
/// Rejects: unknown status (`UnknownStatus`); a `tag` invalid for the status
/// (`UnknownResponseTag`); `ptr + len` overflow (`LengthOverflow`).
pub fn decode_response(buf: &[u8]) -> Result<DecodedResponse, Reason> {
    let mut cur = Cursor::new(buf, limits::MAX_RESPONSE_BYTES);
    let status_raw = cur.take_u32()?;
    let tag = cur.take_u32()?;
    let ptr = cur.take_u32()?;
    let len = cur.take_u32()?;

    // Overflow check on the descriptor's own ptr+len (host-kernel rule),
    // before trusting either to address the payload.
    let end = (ptr as u64).checked_add(len as u64).ok_or(Reason::LengthOverflow)?;
    if end > u32::MAX as u64 {
        return Err(Reason::LengthOverflow);
    }

    let status = Status::from_u32(status_raw).ok_or(Reason::UnknownStatus)?;
    validate_response_tag(status, tag)?;

    // Payload immediately follows the 16-byte descriptor in this framing.
    let payload_bytes = &buf[16.min(buf.len())..];
    if payload_bytes.len() as u64 != len as u64 {
        // Declared len must match the bytes actually present after the
        // descriptor in this self-contained framing.
        return Err(Reason::LengthExceedsBuffer);
    }

    // No-payload statuses carry an empty payload -> decode as a sentinel.
    let payload = if len == 0 {
        Value::Null
    } else {
        let mut pcur = Cursor::new(payload_bytes, limits::MAX_RESPONSE_BYTES);
        let v = decode_value_from(&mut pcur)?;
        if pcur.remaining() != 0 {
            return Err(Reason::TrailingBytes);
        }
        v
    };

    Ok(DecodedResponse { status, tag, payload })
}

/// A `tag` is valid only for specific statuses (the per-status shape enum).
fn validate_response_tag(status: Status, tag: u32) -> Result<(), Reason> {
    let ok = match status {
        Status::Ok => tag == OkShape::Value as u32 || tag == OkShape::ValueWithHandles as u32,
        Status::GuestErrorResponse => tag == 0 || tag == 1, // GuestError | (reserved) HostDiagnostic
        // invalid_request carries the decode-failure reason in tag; any
        // defined reason is acceptable. Other statuses use tag 0.
        Status::InvalidRequest => true,
        _ => tag == 0,
    };
    if ok {
        Ok(())
    } else {
        Err(Reason::UnknownResponseTag)
    }
}

/// Build the 16-byte descriptor bytes for an `AbiResponse`.
pub fn encode_descriptor(r: &AbiResponse) -> [u8; 16] {
    let mut b = [0u8; 16];
    b[0..4].copy_from_slice(&r.status.to_le_bytes());
    b[4..8].copy_from_slice(&r.tag.to_le_bytes());
    b[8..12].copy_from_slice(&r.ptr.to_le_bytes());
    b[12..16].copy_from_slice(&r.len.to_le_bytes());
    b
}

/// Build an `invalid_request` descriptor carrying the decode-failure reason in
/// `tag` (the S4.8 decision). `ptr`/`len` describe an empty payload.
pub fn invalid_request_descriptor(reason: Reason) -> AbiResponse {
    AbiResponse { status: Status::InvalidRequest as u32, tag: reason as u32, ptr: 0, len: 0 }
}
