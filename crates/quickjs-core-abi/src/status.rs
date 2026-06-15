//! Operation-outcome statuses and the AbiResponse descriptor, per
//! `docs/adr/0002-wire-codec.md`. Distinct from decode-failure *reasons*
//! (see `reason.rs`): statuses are guest-written wire values reporting how an
//! operation turned out; reasons are decoder-side classifications.

/// Operation-outcome status — the `AbiResponse.status` enum.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u32)]
pub enum Status {
    Ok = 0,
    GuestErrorResponse = 1,
    InvalidRequest = 2,
    InvalidRuntime = 3,
    InvalidContext = 4,
    InvalidHandle = 5,
    Unsupported = 6,
    ResourceExhausted = 7,
    GuestPanic = 8,
    AbiMismatch = 9,
    Timeout = 10,
    StackOverflow = 11,
    Deadlock = 12,
}

impl Status {
    pub fn from_u32(v: u32) -> Option<Status> {
        use Status::*;
        Some(match v {
            0 => Ok,
            1 => GuestErrorResponse,
            2 => InvalidRequest,
            3 => InvalidRuntime,
            4 => InvalidContext,
            5 => InvalidHandle,
            6 => Unsupported,
            7 => ResourceExhausted,
            8 => GuestPanic,
            9 => AbiMismatch,
            10 => Timeout,
            11 => StackOverflow,
            12 => Deadlock,
            _ => return None,
        })
    }
}

/// The fixed 16-byte response descriptor: `status, tag, ptr, len` (each u32,
/// little-endian on the wire). `tag` is a per-status payload-shape enum;
/// under `InvalidRequest` it carries the decode-failure reason.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AbiResponse {
    pub status: u32,
    pub tag: u32,
    pub ptr: u32,
    pub len: u32,
}

/// Payload shapes under `status = Ok` (the `tag` enum for that status).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u32)]
pub enum OkShape {
    /// A value tree, no handle leaves, nothing to dispose.
    Value = 0,
    /// A value tree carrying handle leaves the host owns and must dispose.
    ValueWithHandles = 1,
}
