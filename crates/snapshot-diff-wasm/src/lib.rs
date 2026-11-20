//! Bounded, single-threaded qbsdiff WASI guest; see README for the host contract.

#![deny(unsafe_op_in_unsafe_fn)]

use std::cell::RefCell;
use std::io::{self, Write};

use qbsdiff::{Bsdiff, Bspatch, ParallelScheme};

const MAX_SNAPSHOT: usize = 256 * 1024 * 1024;
const MAX_ALLOCATION: usize = 2 * MAX_SNAPSHOT + 4096;
const FAILURE: u32 = 1;
type GuestResult<T> = Result<T, &'static str>;

struct Allocation {
    bytes: Vec<u8>,
    logical_len: usize,
}

impl Allocation {
    fn ptr(&self) -> usize {
        self.bytes.as_ptr() as usize
    }
}

struct ErrorBuffer {
    bytes: [u8; 128],
    len: usize,
}

impl Default for ErrorBuffer {
    fn default() -> Self {
        Self {
            bytes: [0; 128],
            len: 0,
        }
    }
}

impl ErrorBuffer {
    fn set(&mut self, message: &str) {
        let bytes = message.as_bytes();
        self.len = bytes.len().min(self.bytes.len());
        self.bytes[..self.len].copy_from_slice(&bytes[..self.len]);
    }

    fn clear(&mut self) {
        self.bytes.fill(0);
        self.len = 0;
    }

    fn is_empty(&self) -> bool {
        self.len == 0
    }
}

#[derive(Default)]
struct State {
    allocations: Vec<Allocation>,
    result: Vec<u8>,
    error: ErrorBuffer,
}

impl State {
    fn clear(&mut self) {
        self.result = Vec::new();
        self.error.clear();
    }

    fn allocate(&mut self, len: usize) -> GuestResult<usize> {
        if len > MAX_ALLOCATION {
            return Err("allocation exceeds hard limit");
        }
        self.allocations
            .try_reserve(1)
            .map_err(|_| "allocation registry exhausted")?;
        let mut bytes = Vec::new();
        let physical_len = len.max(1);
        bytes
            .try_reserve_exact(physical_len)
            .map_err(|_| "input allocation failed")?;
        bytes.resize(physical_len, 0);
        let allocation = Allocation {
            bytes,
            logical_len: len,
        };
        let ptr = allocation.ptr();
        self.allocations.push(allocation);
        Ok(ptr)
    }

    fn free(&mut self, ptr: usize, len: usize) {
        if let Some(index) = self
            .allocations
            .iter()
            .position(|allocation| allocation.ptr() == ptr && allocation.logical_len == len)
        {
            self.allocations.swap_remove(index);
        }
    }

    fn input(&self, ptr: usize, len: usize, limit: usize) -> GuestResult<&[u8]> {
        if len > limit {
            return Err("input exceeds hard limit");
        }
        let allocation = self
            .allocations
            .iter()
            .find(|allocation| allocation.ptr() == ptr && allocation.logical_len == len)
            .ok_or("input is not an exact guest-owned allocation")?;
        Ok(&allocation.bytes[..len])
    }

    fn finish(&mut self, outcome: GuestResult<Vec<u8>>) -> u32 {
        match outcome {
            Ok(result) => {
                self.result = result;
                self.error.clear();
                0
            }
            Err(error) => {
                self.result = Vec::new();
                self.error.set(error);
                FAILURE
            }
        }
    }

    fn diff(
        &mut self,
        srcptr: usize,
        srclen: usize,
        dstptr: usize,
        dstlen: usize,
        max_patch_bytes: usize,
    ) -> u32 {
        self.clear();
        let outcome = (|| {
            let source = self.input(srcptr, srclen, MAX_SNAPSHOT)?;
            let target = self.input(dstptr, dstlen, MAX_SNAPSHOT)?;
            make_diff(source, target, max_patch_bytes)
        })();
        self.finish(outcome)
    }

    fn patch(
        &mut self,
        srcptr: usize,
        srclen: usize,
        patchptr: usize,
        patchlen: usize,
        max_output_bytes: usize,
    ) -> u32 {
        self.clear();
        let outcome = (|| {
            let source = self.input(srcptr, srclen, MAX_SNAPSHOT)?;
            let patch = self.input(patchptr, patchlen, MAX_ALLOCATION)?;
            apply_patch(source, patch, max_output_bytes)
        })();
        self.finish(outcome)
    }
}

struct BoundedWriter {
    bytes: Vec<u8>,
    limit: usize,
}

impl BoundedWriter {
    fn new(limit: usize) -> Self {
        Self {
            bytes: Vec::new(),
            limit,
        }
    }
}

impl Write for BoundedWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let new_len = self
            .bytes
            .len()
            .checked_add(bytes.len())
            .filter(|&len| len <= self.limit)
            .ok_or(io::ErrorKind::FileTooLarge)?;
        self.bytes
            .try_reserve_exact(new_len - self.bytes.len())
            .map_err(|_| io::ErrorKind::OutOfMemory)?;
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn make_diff(source: &[u8], target: &[u8], max_patch_bytes: usize) -> GuestResult<Vec<u8>> {
    if source.len() > MAX_SNAPSHOT || target.len() > MAX_SNAPSHOT {
        return Err("snapshot exceeds hard limit");
    }
    let mut output = BoundedWriter::new(max_patch_bytes.min(MAX_ALLOCATION));
    Bsdiff::new(source, target)
        .parallel_scheme(ParallelScheme::Never)
        .compare(&mut output)
        .map_err(|_| "diff failed or patch output limit exceeded")?;
    Ok(output.bytes)
}

fn header_size(bytes: &[u8]) -> GuestResult<u64> {
    let bytes: [u8; 8] = bytes.try_into().map_err(|_| "truncated header size")?;
    if bytes[7] & 0x80 != 0 {
        return Err("negative BSDIFF40 header size");
    }
    Ok(u64::from_le_bytes(bytes))
}

fn validate_header(patch: &[u8], max_output_bytes: usize) -> GuestResult<usize> {
    if patch.len() < 32 || &patch[..8] != b"BSDIFF40" {
        return Err("invalid or truncated BSDIFF40 header");
    }
    let control_size = header_size(&patch[8..16])?;
    let delta_size = header_size(&patch[16..24])?;
    let target_size = header_size(&patch[24..32])?;
    let blocks_end = 32_u64
        .checked_add(control_size)
        .and_then(|size| size.checked_add(delta_size))
        .ok_or("BSDIFF40 block sizes overflow")?;
    if blocks_end > patch.len() as u64 {
        return Err("BSDIFF40 blocks exceed patch length");
    }
    let target_size =
        usize::try_from(target_size).map_err(|_| "target size exceeds address space")?;
    if target_size > max_output_bytes.min(MAX_SNAPSHOT) {
        return Err("target size exceeds output limit");
    }
    Ok(target_size)
}

fn apply_patch(source: &[u8], patch: &[u8], max_output_bytes: usize) -> GuestResult<Vec<u8>> {
    if source.len() > MAX_SNAPSHOT || patch.len() > MAX_ALLOCATION {
        return Err("input exceeds hard limit");
    }
    let expected = validate_header(patch, max_output_bytes)?;
    let mut output = BoundedWriter::new(expected);
    let count = Bspatch::new(patch)
        .map_err(|_| "qbsdiff rejected patch header")?
        .apply(source, &mut output)
        .map_err(|_| "patch failed or declared output length exceeded")?;
    if count != expected as u64 || output.bytes.len() != expected {
        return Err("patch output differs from declared target length");
    }
    Ok(output.bytes)
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

/// Allocate a zero-initialized input buffer; zero means failure.
#[no_mangle]
pub extern "C" fn qjsd_alloc(len: u32) -> u32 {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        state.clear();
        match state.allocate(len as usize) {
            Ok(ptr) => match u32::try_from(ptr) {
                Ok(ptr) => ptr,
                Err(_) => {
                    state.free(ptr, len as usize);
                    state
                        .error
                        .set("allocation pointer exceeds ABI address space");
                    0
                }
            },
            Err(error) => {
                state.error.set(error);
                0
            }
        }
    })
}

/// Free only an exact guest-owned pointer and logical length; otherwise no-op.
#[no_mangle]
pub extern "C" fn qjsd_free(ptr: u32, len: u32) {
    STATE.with(|state| state.borrow_mut().free(ptr as usize, len as usize));
}

/// Generate BSDIFF40; only status zero is success.
#[no_mangle]
pub extern "C" fn qjsd_diff(
    srcptr: u32,
    srclen: u32,
    dstptr: u32,
    dstlen: u32,
    max_patch_bytes: u32,
) -> u32 {
    STATE.with(|state| {
        state.borrow_mut().diff(
            srcptr as usize,
            srclen as usize,
            dstptr as usize,
            dstlen as usize,
            max_patch_bytes as usize,
        )
    })
}

/// Apply BSDIFF40; only status zero is success.
#[no_mangle]
pub extern "C" fn qjsd_patch(
    srcptr: u32,
    srclen: u32,
    patchptr: u32,
    patchlen: u32,
    max_output_bytes: u32,
) -> u32 {
    STATE.with(|state| {
        state.borrow_mut().patch(
            srcptr as usize,
            srclen as usize,
            patchptr as usize,
            patchlen as usize,
            max_output_bytes as usize,
        )
    })
}

/// Pointer to the current result, or zero when empty.
#[no_mangle]
pub extern "C" fn qjsd_result_ptr() -> u32 {
    STATE.with(|state| {
        let state = state.borrow();
        if state.result.is_empty() {
            0
        } else {
            state.result.as_ptr() as usize as u32
        }
    })
}

#[no_mangle]
pub extern "C" fn qjsd_result_len() -> u32 {
    STATE.with(|state| state.borrow().result.len() as u32)
}

/// Pointer to the owned UTF-8 error buffer, or zero when absent.
#[no_mangle]
pub extern "C" fn qjsd_error_ptr() -> u32 {
    STATE.with(|state| {
        let state = state.borrow();
        if state.error.is_empty() {
            0
        } else {
            state.error.bytes.as_ptr() as usize as u32
        }
    })
}

#[no_mangle]
pub extern "C" fn qjsd_error_len() -> u32 {
    STATE.with(|state| state.borrow().error.len as u32)
}

/// Release result and error state; input allocations are unaffected.
#[no_mangle]
pub extern "C" fn qjsd_result_free() {
    STATE.with(|state| state.borrow_mut().clear());
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header(control: u64, delta: u64, target: u64) -> Vec<u8> {
        let mut patch = b"BSDIFF40".to_vec();
        for size in [control, delta, target] {
            patch.extend_from_slice(&size.to_le_bytes());
        }
        patch
    }

    fn input(state: &mut State, bytes: &[u8]) -> usize {
        let ptr = state.allocate(bytes.len()).unwrap();
        let allocation = state.allocations.last_mut().unwrap();
        allocation.bytes[..bytes.len()].copy_from_slice(bytes);
        ptr
    }

    #[test]
    fn normal_round_trip() {
        let source = b"snapshot memory with old value and a shared tail";
        let target = b"snapshot memory with NEW value and a shared tail!";
        let patch = make_diff(source, target, MAX_ALLOCATION).unwrap();
        assert_eq!(apply_patch(source, &patch, target.len()).unwrap(), target);
    }

    #[test]
    fn empty_round_trips() {
        for (source, target) in [
            (&b""[..], &b""[..]),
            (&b""[..], &b"nonempty"[..]),
            (&b"nonempty"[..], &b""[..]),
        ] {
            let patch = make_diff(source, target, MAX_ALLOCATION).unwrap();
            assert_eq!(apply_patch(source, &patch, target.len()).unwrap(), target);
        }
    }

    #[test]
    fn negative_header_sizes_including_negative_zero() {
        for field in 0..3 {
            for magnitude in [0, 1, i64::MAX as u64] {
                let mut patch = header(0, 0, 0);
                patch[8 + field * 8..16 + field * 8]
                    .copy_from_slice(&(magnitude | (1 << 63)).to_le_bytes());
                assert!(apply_patch(b"", &patch, MAX_SNAPSHOT).is_err());
            }
        }
    }

    #[test]
    fn overflow_sized_and_truncated_headers() {
        for patch in [
            header(i64::MAX as u64, i64::MAX as u64, 0),
            header(u32::MAX as u64, 32, 0),
            header(1, 0, 0),
            header(0, 1, 0),
            header(0, 0, i64::MAX as u64),
        ] {
            assert!(apply_patch(b"", &patch, usize::MAX).is_err());
        }
        let valid = header(0, 0, 0);
        for len in 0..32 {
            assert!(apply_patch(b"", &valid[..len], MAX_SNAPSHOT).is_err());
        }
        let mut invalid_magic = valid;
        invalid_magic[0] = b'X';
        assert!(apply_patch(b"", &invalid_magic, MAX_SNAPSHOT).is_err());
    }

    #[test]
    fn declared_target_must_match_actual_length() {
        let mut patch = make_diff(b"before", b"after!", MAX_ALLOCATION).unwrap();
        for wrong_size in [0_u64, 5, 7, 1024] {
            patch[24..32].copy_from_slice(&wrong_size.to_le_bytes());
            assert!(apply_patch(b"before", &patch, MAX_SNAPSHOT).is_err());
        }
    }

    #[test]
    fn output_limits_and_writer_checks() {
        let patch = make_diff(b"before", b"after!", MAX_ALLOCATION).unwrap();
        assert!(apply_patch(b"before", &patch, 5).is_err());
        assert!(make_diff(b"before", b"after!", patch.len() - 1).is_err());
        assert!(make_diff(b"", b"", 0).is_err());
        assert!(validate_header(&header(0, 0, (MAX_SNAPSHOT + 1) as u64), usize::MAX).is_err());
        let mut writer = BoundedWriter::new(3);
        writer.write_all(b"abc").unwrap();
        assert!(writer.write_all(b"d").is_err());
        assert_eq!(writer.bytes, b"abc");
    }

    #[test]
    fn allocation_ownership_and_invalid_free() {
        let mut state = State::default();
        let ptr = input(&mut state, b"abc");
        assert!(state.input(ptr + 1, 2, MAX_SNAPSHOT).is_err());
        assert!(state.input(ptr, 2, MAX_SNAPSHOT).is_err());
        assert!(state.input(0, 0, MAX_SNAPSHOT).is_err());
        state.free(ptr, 2);
        state.free(ptr + 1, 3);
        state.free(0, 0);
        assert_eq!(state.input(ptr, 3, MAX_SNAPSHOT).unwrap(), b"abc");
        state.free(ptr, 3);
        state.free(ptr, 3);
        assert!(state.input(ptr, 3, MAX_SNAPSHOT).is_err());
        assert!(state.allocate(MAX_ALLOCATION + 1).is_err());
    }

    #[test]
    fn zero_length_allocations_have_distinct_real_storage() {
        let mut state = State::default();
        let first = state.allocate(0).unwrap();
        let second = state.allocate(0).unwrap();
        assert_ne!(first, 0);
        assert_ne!(first, second);
        assert_eq!(state.input(first, 0, MAX_SNAPSHOT).unwrap(), b"");
        state.free(first, 1);
        assert_eq!(state.allocations.len(), 2);
        state.free(first, 0);
        state.free(second, 0);
        assert!(state.allocations.is_empty());
    }

    #[test]
    fn operations_clear_stale_results_and_errors_and_allow_shared_input() {
        let mut state = State::default();
        let ptr = input(&mut state, b"same");
        assert_eq!(state.diff(ptr, 4, ptr, 4, MAX_ALLOCATION), 0);
        let patch = state.result.clone();
        assert_eq!(state.diff(ptr, 3, ptr, 4, MAX_ALLOCATION), FAILURE);
        assert!(state.result.is_empty());
        assert!(!state.error.is_empty());
        let patchptr = input(&mut state, &patch);
        assert_eq!(state.patch(ptr, 4, patchptr, patch.len(), 4), 0);
        assert_eq!(state.result, b"same");
        assert!(state.error.is_empty());
        assert_eq!(state.patch(ptr, 4, patchptr, patch.len(), 3), FAILURE);
        assert!(state.result.is_empty());
        state.clear();
        assert!(state.error.is_empty());
        assert_eq!(state.allocations.len(), 2);
    }

    #[test]
    fn truncated_required_compressed_data_fails_closed() {
        let patch = make_diff(b"", b"after!", MAX_ALLOCATION).unwrap();
        let control_end = 32 + header_size(&patch[8..16]).unwrap() as usize;
        let delta_end = control_end + header_size(&patch[16..24]).unwrap() as usize;
        for len in [32, control_end, delta_end, delta_end + 4] {
            assert!(apply_patch(b"", &patch[..len], MAX_SNAPSHOT).is_err());
        }
    }

    /// A reference patch can add against zero bytes beyond the source; qbsdiff
    /// instead returns UnexpectedEof. Preserve this rejection without a decoder.
    #[test]
    fn reference_out_of_source_zero_extension_is_rejected() {
        let patch = make_diff(b"abcd", b"abce", MAX_ALLOCATION).unwrap();
        assert!(apply_patch(b"", &patch, 4).is_err());
    }
}
