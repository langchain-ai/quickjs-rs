//! Guest memory exports: the host allocates request buffers in guest linear
//! memory, writes bytes in, calls an export, then frees. `docs/adr/0002` /
//! spec Memory Management Exports.
//!
//! Length + align are passed back on free (the ABI is explicit about this) so
//! the guest can reconstruct the exact `Layout` — wasm has no `malloc`-style
//! size header we could rely on, and `std::alloc` requires the same layout
//! for dealloc as alloc. All functions are infallible-signature (return a
//! null/sentinel on failure) and never panic.

use std::alloc::{alloc, dealloc, Layout};

/// Allocate `len` bytes with `align` alignment, returning the guest pointer
/// (as u32 — wasm32 pointers are 32-bit) or 0 on failure (bad layout / OOM).
#[no_mangle]
pub extern "C" fn qrs_alloc(len: u32, align: u32) -> u32 {
    let layout = match layout_of(len, align) {
        Some(l) => l,
        None => return 0,
    };
    if layout.size() == 0 {
        // Zero-size alloc: return a non-null but never-dereferenced sentinel
        // (alignment value), matching std's dangling-but-aligned convention.
        return align.max(1);
    }
    // SAFETY: layout has nonzero size (checked above).
    let p = unsafe { alloc(layout) };
    p as usize as u32
}

/// Free a buffer previously returned by `qrs_alloc`, with the same len/align.
#[no_mangle]
pub extern "C" fn qrs_free(ptr: u32, len: u32, align: u32) {
    let layout = match layout_of(len, align) {
        Some(l) => l,
        None => return,
    };
    if layout.size() == 0 || ptr == 0 {
        return; // nothing was allocated for a zero-size / sentinel request
    }
    // SAFETY: caller guarantees ptr came from qrs_alloc with this same layout
    // (the explicit-len/align ABI contract).
    unsafe { dealloc(ptr as usize as *mut u8, layout) }
}

fn layout_of(len: u32, align: u32) -> Option<Layout> {
    // align must be a power of two; Layout::from_size_align enforces it.
    Layout::from_size_align(len as usize, align.max(1) as usize).ok()
}

/// Copy `len` bytes out of guest memory at `ptr` into an owned Vec. Used
/// internally to read a request the host wrote via qrs_alloc. Returns None on
/// a null/zero-length combination that would be unsound to read.
pub(crate) fn read_guest(ptr: u32, len: u32) -> Option<Vec<u8>> {
    if len == 0 {
        return Some(Vec::new());
    }
    if ptr == 0 {
        return None;
    }
    // SAFETY: the host wrote `len` bytes here via qrs_alloc; we copy them out.
    let slice = unsafe { std::slice::from_raw_parts(ptr as usize as *const u8, len as usize) };
    Some(slice.to_vec())
}

/// Allocate guest memory and copy `bytes` into it, returning (ptr, len) for a
/// response the host will read then free with qrs_response_free. Returns
/// (0, 0) on failure.
pub(crate) fn write_guest(bytes: &[u8]) -> (u32, u32) {
    let len = bytes.len() as u32;
    let ptr = qrs_alloc(len, 1);
    if ptr == 0 && len != 0 {
        return (0, 0);
    }
    if len != 0 {
        // SAFETY: just allocated len bytes at ptr.
        unsafe {
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), ptr as usize as *mut u8, len as usize);
        }
    }
    (ptr, len)
}

/// Free a response buffer (align 1, as written by `write_guest`).
#[no_mangle]
pub extern "C" fn qrs_response_free(ptr: u32, len: u32) {
    qrs_free(ptr, len, 1);
}
