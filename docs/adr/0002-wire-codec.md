# ADR 0002 / Spec: Wire Codec (custom tagged-binary)

Date: 2026-06-12
Status: Draft for annotation
Decision: custom tagged-binary over MessagePack (see "Why custom" below).

> This document is the byte-level contract for values crossing the
> WASM/host boundary. It is meant to be marked up — annotate any field,
> tag value, or rule you want to change. Nothing here is frozen until we
> ratify it into `quickjs-core-abi`.

## Why custom (decision summary)

Under "assume engine compromise," a smaller grammar is a smaller attack
surface. MessagePack's generality (multiple encodings per value, unused
extension/timestamp types, map/object ambiguity) is a liability here:
every flexibility is a place the three host decoders (Rust/Python/TS) can
disagree — protocol confusion — and a place a hostile guest can probe.
A format with exactly one valid byte sequence per value is adversarially
simpler, removes a third-party dependency from the Python wheel and npm
package (helping repro/supply-chain), and lets BigInt-as-decimal and
Handle be first-class. Cost: we write and fuzz three decoders. That cost
is accepted; the differential-fuzz harness keeps them honest.

## Design rules

1. **One canonical encoding per value.** There is exactly one valid byte
   sequence for any value. Decoders MUST reject non-canonical input
   (e.g. a length that could have been shorter, trailing bytes, an
   unknown tag) with `invalid_request` — not best-effort parse it.
2. **Little-endian**, fixed-width integers (no varints — varints add a
   second canonicalization concern; fixed width is simpler to validate).
3. **Length-prefixed**, never delimited. Every variable-length field is
   preceded by its `u32` byte length.
4. **Untrusted input, fail closed.** Every length is bounds-checked
   against the remaining buffer with overflow checks (`offset + len` must
   not wrap and must not exceed the buffer). Any failure is terminal.
5. **Bounded.** Nesting depth and total size are capped (see Limits);
   decoders are iterative or depth-counted, never blindly recursive.
6. **Debug-JSON mode** exists for tests/diagnostics only; it is never the
   wire format and never enabled on the hot path.

## Value model — wire format

Each value is `tag (1 byte) || body`. Tags:

| Tag | Variant | Body |
|----:|---------|------|
| `0x00` | Null | (none) |
| `0x01` | Undefined | (none) |
| `0x02` | Bool(false) | (none) — value folded into the tag |
| `0x03` | Bool(true) | (none) |
| `0x04` | Number(f64) | 8 bytes, IEEE-754 little-endian |
| `0x05` | BigInt | `len: u32` then `len` bytes UTF-8 decimal string (e.g. `-170141183460469231731687303715884105728`) |
| `0x06` | String | `len: u32` then `len` bytes UTF-8 |
| `0x07` | Bytes | `len: u32` then `len` raw bytes |
| `0x08` | Array | `count: u32` then `count` encoded values |
| `0x09` | Object | `count: u32` then `count` pairs of (`key`: String-body `len:u32`+utf8, `value`: encoded value); insertion order preserved |
| `0x0A` | Handle | `context_id: u32`, `handle_id: u32`, `generation: u32`, `type_name`: `len:u32`+utf8 |
| `0x0B` | Error | `name`, `message`, `stack` each as `len:u32`+utf8; `stack` len may be 0 (absent) |

Notes / open annotations:

- **Bool folded into the tag** (0x02/0x03) instead of a 1-byte body — one
  fewer byte, one fewer thing to canonicalize. *Annotate if you'd rather
  keep a single Bool tag + body byte.*
- **Number is always f64**, matching JS. NaN/Infinity are valid f64 bit
  patterns and cross as-is. *Open: do we canonicalize NaN to a single bit
  pattern to keep "one encoding per value"? Leaning yes — quiet NaN.*
- **BigInt as decimal string** (not raw two's-complement) per the spec, to
  avoid sign/width ambiguity and precision loss. Canonical form: no
  leading zeros, leading `-` only for negatives, `0` is `0` not `-0`.
- **Object keys are always String bodies**, never arbitrary values (JS
  property keys are strings or symbols; symbols don't cross — they error
  at marshal, as today).
- **Cycles are not representable** and not supported — the value model is
  a tree. Opaque object graphs use Handle. A guest that produces a cyclic
  structure for marshaling gets a marshal error, not infinite output.
- **No integer tag.** JS numbers are f64; we do not add an int type. This
  is deliberate (avoids the MessagePack "which int width" ambiguity).
  *Annotate if array-index/length-heavy payloads make a u32 fast-path
  worth the extra canonicalization rule.*

## Limits (decoder caps, enforced before/while parsing)

These mirror the Resource Controls table; the codec restates the ones it
enforces:

| Limit | Default | Rationale |
|-------|---------|-----------|
| Max nesting depth | 128 | matches current `MAX_MARSHAL_DEPTH`; preserves native parity |
| Max total payload | 32 MiB (response) / 8 MiB (host-call arg) | matches Resource Controls caps |
| Max string/bytes length | bounded by total payload | no separate cap; the total bounds it |
| Max array/object count | bounded by total payload | a `count` of 2^32-1 fails the bounds check immediately |

A `count` or `len` that exceeds the remaining buffer is rejected before
any allocation — decoders never trust a length to size an allocation.

## Request/Response envelope

Envelope (request):

```
abi_version : u32
request_id  : u32        # correlation; debugging + async settlement
kind        : u32        # which operation (eval, handle_get, ...)
flags       : u32        # per-kind bitflags (e.g. EVAL_FLAG_HANDLE_RESULT)
payload     : Value      # operation-specific, encoded per the value model
```

Response payload rides the `AbiResponse` descriptor (below); the decoded
body is a `Value` (or an `Error` value when `status` indicates a JS
exception).

## AbiResponse descriptor

Fixed 16-byte struct written to guest memory / out-pointer:

```
status : u32
tag    : u32     # disambiguates payload shape for this status
ptr    : u32     # guest linear-memory offset of the payload
len    : u32     # payload length in bytes
```

Hosts validate `ptr + len` against linear memory with overflow checks
before reading (Host Adapter Security Requirements).

## Status codes

```
0  ok
1  guest_error_response   # JS threw; payload is an Error value
2  invalid_request        # malformed/non-canonical wire input, bad request_id
3  invalid_runtime
4  invalid_context
5  invalid_handle
6  unsupported
7  resource_exhausted     # a cap was hit (memory, handles, pending, size)
8  guest_panic            # guest Rust panicked; instance poisoned
9  abi_mismatch
10 timeout                # deadline enforcement (added: classified distinctly)
11 stack_overflow         # recursion/stack trap (added: classified distinctly)
```

`timeout` and `stack_overflow` are new vs the spec's original list —
added because our review decided both must be classified distinctly from
a generic trap/panic so hosts surface `TimeoutError` / `StackOverflow`
rather than `guest_panic`.

## Versioning rule

`quickjs-core-abi` is a versioned crate. `qrs_abi_version()` returns its
integer version. Any wire-incompatible change (new/removed/reordered tag,
changed field width, changed envelope) bumps the major ABI version;
additive feature-flagged changes bump minor. The guest and every host
fail fast (`abi_mismatch`) on a version they don't support — no
best-effort cross-version parsing.

## Conformance / fuzzing obligations

- **Differential fuzz** across the three codecs: identical input → identical
  decoded value or identical rejection, in Rust, Python, and TS. Any
  divergence fails CI.
- **Canonical-form fuzz**: mutated/non-canonical encodings (over-long
  lengths, trailing bytes, unknown tags, out-of-bounds counts) MUST be
  rejected by all three, never silently accepted.
- **Round-trip**: `decode(encode(v)) == v` for the full value model,
  including BigInt edge values, empty containers, and deep (depth-128)
  nesting.

## Open questions for annotation

1. NaN canonicalization — fix to quiet NaN, or pass f64 bits through?
2. Bool-in-tag vs Bool tag + body byte?
3. Any need for a u32 integer fast-path, or is f64-only final?
4. Are the two added status codes (`timeout`, `stack_overflow`) the right
   split, or do we also want a distinct `deadlock` code (the eval state
   machine has a Deadlock poll state)?
5. `request_id` width — u32 enough, or u64 for long-lived multiplexed
   instances?
