# Snapshot diff WASI guest

Standalone Cargo workspace using the exact registry dependency `qbsdiff = "=1.4.4"`.
No vendored code, alternative diff/patch algorithm, QuickJS integration, or Python
status enum is included. The release artifact is
`target/wasm32-wasip1/release/snapshot_diff_wasm.wasm`.

## ABI

All parameters and return values below are `u32`, except `void` returns. WASM
represents these as `i32`; hosts must interpret addresses and lengths as unsigned.

```text
qjsd_alloc(len) -> pointer
qjsd_free(ptr, len) -> void
qjsd_diff(srcptr, srclen, dstptr, dstlen, max_patch_bytes) -> status
qjsd_patch(srcptr, srclen, patchptr, patchlen, max_output_bytes) -> status
qjsd_result_ptr() -> pointer
qjsd_result_len() -> length
qjsd_error_ptr() -> pointer
qjsd_error_len() -> length
qjsd_result_free() -> void
```

`qjsd_alloc` returns zero on failure, with an error message. Other allocation
pointers are guest-owned, zero-initialized buffers, including a real one-byte
allocation for logical length zero. Freeing requires the exact pointer and
logical length; unknown pointers, wrong lengths, and repeated frees are no-ops.
Input reads require that same exact registered pointer and length. Shared input
allocations are allowed; arbitrary host-provided memory ranges are not borrowed.
No unsafe Rust blocks or raw-pointer slice construction are used.

Only operation status **0** means success. Any nonzero status (currently 1) means
failure: read the guest-owned UTF-8 error buffer. Error text is diagnostic, not a
stable machine-readable category. On failure there is no result. Successful
empty results have pointer and length zero. Accessors never transfer ownership.
Copy results/errors before calling `qjsd_alloc`, `qjsd_diff`, `qjsd_patch`, or
`qjsd_result_free`, all of which invalidate previous result/error state.
`qjsd_result_free` clears both buffers without freeing input allocations.

## Bounds and host obligations

- Maximum source, target snapshot, and patched output: 256 MiB.
- Maximum single input allocation, incoming patch, and generated patch:
  `2 * 256 MiB + 4096` bytes (536,875,008 bytes).
- Caller output limits are applied in addition to hard caps, including zero.
- Guest allocations and output growth use fallible reservation. Error storage is
  an owned fixed-size buffer, so recording an error needs no allocation.
- Diff explicitly uses `ParallelScheme::Never`.
- Before calling qbsdiff's patch parser, the guest validates `BSDIFF40`, the
  32-byte minimum header, nonnegative sign-magnitude sizes (negative zero is
  rejected), checked block-size addition and patch bounds, and target-size limits.
  It never trusts `hint_target_size` or preallocates from an unchecked header.
- Patch output is incrementally bounded by the validated header target length;
  both qbsdiff's returned count and actual bytes must equal that length.

The host **must configure fuel and linear-memory bounds before any guest call**
and use a fresh store per operation. Dependency-internal allocations are not all
fallible. OOM, unexpected panics, fuel exhaustion, and any other traps must be
mapped to host errors and the store discarded. Header validation and the bounded
writer are not replacements for host resource limits. In particular, hostile
compressed controls can consume CPU without producing output; there is no second
control-stream decoder here. WASI preview1 imports must be supplied; no filesystem
preopens or inherited environment are needed.

qbsdiff rejects reference BSDIFF40 patches requiring zero-extension outside the
source. This is intentionally preserved and tested, not worked around with a
custom patcher. Compressed-stream validation otherwise follows qbsdiff: it can
accept a missing bzip2 trailer after all required output has been read. This guest
does not independently decode streams or promise canonical compressed encoding.
Normal dependency I/O errors fail closed, and no partial result is exposed.

## Build

Build with `cargo build --locked --release --target wasm32-wasip1`; C dependencies
need a WASI-compatible Clang/sysroot. The package build script bundles the artifact.
Dependency license texts are included in `quickjs_rs/THIRD_PARTY_LICENSES.txt`.
