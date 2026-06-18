//! Linear-memory allocation + the pending result buffer.
//!
//! The host calls `qjs_alloc` to obtain a buffer it writes input into,
//! passes the ptr/len to an export, then frees it with `qjs_free`. The
//! result buffer is the one-slot channel for value-extraction output
//! (`get_string`/`get_bigint`/`get_arraybuffer`/`type_of`) the host reads via
//! `qjs_last_ptr`/`qjs_last_len`.

use std::alloc::{self, Layout};
use std::cell::RefCell;

thread_local! {
    /// Most recent result byte buffer, owned by the guest until the host
    /// reads it and calls `qjs_result_free`. One-slot: each result-producing
    /// call overwrites the previous (host reads eagerly).
    static RESULT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

/// Allocate `size` bytes; returns a pointer (offset) or null on failure/zero.
#[no_mangle]
pub extern "C" fn qjs_alloc(size: usize) -> *mut u8 {
    if size == 0 {
        return std::ptr::null_mut();
    }
    let layout = match Layout::from_size_align(size, 1) {
        Ok(l) => l,
        Err(_) => return std::ptr::null_mut(),
    };
    // SAFETY: non-zero size checked above.
    unsafe { alloc::alloc(layout) }
}

/// Free a buffer from `qjs_alloc`. `size` must match the allocating call.
#[no_mangle]
pub extern "C" fn qjs_free(ptr: *mut u8, size: usize) {
    if ptr.is_null() || size == 0 {
        return;
    }
    if let Ok(layout) = Layout::from_size_align(size, 1) {
        // SAFETY: caller guarantees (ptr, size) came from qjs_alloc.
        unsafe { alloc::dealloc(ptr, layout) };
    }
}

/// Pointer to the pending result buffer (valid until `qjs_result_free`).
#[no_mangle]
pub extern "C" fn qjs_last_ptr() -> *const u8 {
    RESULT.with(|r| r.borrow().as_ptr())
}

/// Length of the pending result buffer.
#[no_mangle]
pub extern "C" fn qjs_last_len() -> usize {
    RESULT.with(|r| r.borrow().len())
}

/// Release the pending result buffer.
#[no_mangle]
pub extern "C" fn qjs_result_free() {
    RESULT.with(|r| {
        let mut buf = r.borrow_mut();
        buf.clear();
        buf.shrink_to_fit();
    });
}

/// Store bytes into the pending result slot (internal).
pub fn set_result(bytes: Vec<u8>) {
    RESULT.with(|r| *r.borrow_mut() = bytes);
}

/// Stage a name byte-slice + an argv i32 array in stable heap buffers and
/// hand their pointers to `f` (the host_call). The buffers live for the
/// duration of `f` and are freed on return.
///
/// We use owned `Vec`s (not the linear-memory allocator exports) because the
/// pointers only need to be valid *within this synchronous call* and into our
/// own address space — the host reads them during `host_call` and copies
/// out. Returns whatever `f` returns.
pub fn with_scratch<R>(
    name: &[u8],
    argv: &[i32],
    f: impl FnOnce(*const u8, usize, *const i32, u32) -> R,
) -> R {
    // Copy into owned buffers so the pointers are stable for the call.
    let name_buf = name.to_vec();
    let argv_buf = argv.to_vec();
    let r = f(
        name_buf.as_ptr(),
        name_buf.len(),
        argv_buf.as_ptr(),
        argv_buf.len() as u32,
    );
    drop(name_buf);
    drop(argv_buf);
    r
}

/// Free a `*const u8` allocation from the modules host imports (which return
/// const ptrs). Casts to `*mut u8` — safe because the memory came from
/// `qjs_alloc` (our own allocator) and we are the sole owner.
pub fn qjs_free_raw(ptr: *const u8, size: usize) {
    if ptr.is_null() || size == 0 {
        return;
    }
    if let Ok(layout) = std::alloc::Layout::from_size_align(size, 1) {
        unsafe { std::alloc::dealloc(ptr as *mut u8, layout) };
    }
}

/// Validate and borrow an untrusted input slice the host wrote in.
///
/// Returns `None` for a null pointer or zero length. A read past the
/// linear-memory bound traps deterministically — contained by the sandbox,
/// never UB in the host (the host side also bound-checks before writing).
pub fn read_input<'a>(ptr: *const u8, len: usize) -> Option<&'a [u8]> {
    if ptr.is_null() || len == 0 {
        return None;
    }
    // SAFETY: host allocated (ptr, len) via qjs_alloc and wrote it; the slice
    // lives until the matching qjs_free after this synchronous call returns.
    Some(unsafe { std::slice::from_raw_parts(ptr, len) })
}
