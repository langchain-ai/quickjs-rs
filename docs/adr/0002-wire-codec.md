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
package (helping repro/supply-chain), and lets BigInt-as-decimal be
first-class. Host-specific ergonomics (e.g. typed handle wrappers) stay
in the host adapter, not the shared codec. Cost: we write and fuzz three
decoders. That cost is accepted; the differential-fuzz harness keeps
them honest.

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
   wire format and never enabled on the hot path. It is also the
   **conformance assertion target** (see Debug-JSON representation): the
   abstract value model, host-neutral, that every decoder must produce.

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
| `0x0A` | Handle | `context_id: u32`, `handle_id: u32`, `generation: u32` (fixed 12 bytes, no body length) |
| `0x0B` | Error | `name`, `message`, `stack` each as `len:u32`+utf8; `stack` len may be 0 (absent) |

Notes / open annotations:

- **Bool folded into the tag** (0x02/0x03) instead of a 1-byte body —
  one fewer byte, and no body byte to canonicalize (a tag+body form would
  add a "body must be 0 or 1, reject otherwise" rule). Ratified 2026-06-13.
- **Number is always f64** (8 bytes, IEEE-754 little-endian), matching JS.
  Two f64 edge cases need explicit canonical-form rules, because "8 raw
  bytes" alone does not give one-encoding-per-value:
  - **NaN is canonicalized.** IEEE-754 has many NaN bit patterns; JS
    exposes only a single observable `NaN`. Encoders MUST emit the
    canonical quiet NaN `0x7FF8000000000000` for any NaN value. Decoders
    MUST treat any received NaN-class pattern (exponent all ones, nonzero
    mantissa) as `NaN`, but — per the reject-non-canonical rule — MUST
    reject a NaN-class pattern that is not exactly the canonical one with
    `invalid_request`. (Rationale: without this, the three codecs could
    emit different NaN bits for the same value, a false differential-fuzz
    divergence. Pass-through would only preserve NaN-payload bits, which
    JS semantics never expose and we have no use for.)
  - **Signed zero is preserved.** `+0.0` (`0x0000000000000000`) and
    `-0.0` (`0x8000000000000000`) are distinct encodings and both valid;
    they are NOT canonicalized to one. JS observes the difference
    (`Object.is(-0, 0) === false`, `1 / -0 === -Infinity`), so collapsing
    them would lose a value distinction.
  - Infinities (`±Infinity`) and all finite f64 values cross as their
    exact bit pattern; there is one bit pattern per such value, so no
    extra rule is needed.
- **BigInt as decimal string** (not raw two's-complement) per the spec, to
  avoid sign/width ambiguity and precision loss. Canonical form: no
  leading zeros, leading `-` only for negatives, `0` is `0` not `-0`.
- **Array/Object are count-prefixed, not byte-length-prefixed** (decided
  2026-06-12). A container's byte extent is defined implicitly by
  decoding its `count` children; nesting is recursion in the grammar (an
  Object's value field is itself a full encoded value, to any depth). We
  always fully decode and never skip subtrees, so a redundant byte-length
  prefix would only add a "prefix must equal real extent" canonicalization
  burden for no use case. The depth cap and per-length bounds-check
  (below) cover the recursion/oversize attack surface.
- **Object keys are a String *body* (`len:u32` + utf8), with no value
  tag** — the key is always a string, so a tag would be redundant. A
  consequence: there is no "non-string key" byte sequence to reject (a
  key is structurally always a string), so no such rejection reason
  exists. (Surfaced by the conformance corpus.) Object keys are never
  arbitrary values (JS
  property keys are strings or symbols; symbols don't cross — they error
  at marshal, as today).
- **Handle is the identity triple only** — `(context_id, handle_id,
  generation)`, no type name on the wire. The triple is the unforgeable,
  context-scoped, reuse-safe identity (generation defeats slot-reuse/ABA
  confusion); no raw guest pointer or `JSValue` ever crosses. Type is a
  host-side, on-demand concern via `qrs_handle_type_of` (cached on the
  host wrapper), not a codec field: it served only host-SDK ergonomics
  (e.g. a TS API wrapping Promise/function/Array handles), which is one
  host's convenience and must not be a burden every host's codec pays —
  Python/Rust hosts have no structural use for it. Removing it also makes
  Handle fixed-width (12 bytes), simpler to encode/validate/fuzz.
  - **Guest validation obligation:** every handle-bearing operation
    validates the full triple against the live handle table and fails
    with `invalid_handle` on any mismatch (wrong context, unknown id,
    stale generation) — never best-effort. This is a decoder/handler
    requirement, not convention.
  - **`qrs_handle_type_of` returns a display string, not a security
    signal:** the guest mints it, so hosts must treat it as descriptive
    only and never branch trust/security decisions on it.
- **Marshalling model (path-based).** The value tree cannot encode
  identity or back-references, so we do not flatten arbitrary object
  graphs into it. Instead:
  - `eval_handle` is the total primitive — always returns a Handle, no
    marshalling, faithful for every JS value (functions, Promises,
    cyclic/shared graphs).
  - `eval` is sugar for `eval_handle` then `to_value`; there is exactly
    **one** marshaller, in `to_value` (`qrs_handle_to_value`).
  - `to_value` is **path-based**: primitives and tree-shaped containers
    copy by value; a reference that revisits a node **already on the
    current ancestor path** is emitted as that node's *existing* Handle
    (not newly minted, not an error); functions/Promises/exotic objects
    are Handles.
  - Consequences:
    - A true **cycle** terminates: the back-edge becomes the referent's
      existing Handle — finite output, identity preserved at the cut.
    - **Shared-but-acyclic** structure (DAGs, `{x: obj, y: obj}`) is
      **duplicated**, not collapsed, because a node leaves the path once
      its subtree completes. This matches the current native deep-copy
      behavior — the reason path-based is the v1 choice. (Visited-based
      dedup, which would collapse all sharing to Handles, was considered
      and deferred as a larger behavior change.)
    - Path tracking is O(depth), bounded by the depth cap above.
    - Therefore an `eval` value tree **may contain Handle leaves** the
      host owns and must dispose; the response flags when it carries
      Handles. The only change vs. native is that a true cycle yields a
      Handle leaf instead of erroring/looping — strictly an improvement.
  - The codec itself is unaware of any of this: it only needs
    Handle-as-a-value (tag `0x0A`). The `eval`/`eval_handle` export
    contract is ABI-surface, specified separately.
- **No integer tag — f64-only, final for V1** (decided 2026-06-13). JS
  numbers are f64; we do not add an int type. A u32/i32 fast-path would
  give the integer `5` two valid encodings (int-tagged and f64-tagged),
  forcing a canonical-boundary rule all three codecs must agree on
  exactly — the MessagePack "which int width" ambiguity through the side
  door, traded for byte savings we have not measured a need for.
  Revisited only if the marshalling benchmark (see spec → Performance
  Benchmarks) shows integer-array marshalling is a real hotspot; if so,
  the fast-path is added then with its canonical-boundary rule designed
  deliberately, not speculatively now.

## Debug-JSON representation (conformance assertion target)

The debug-JSON form is the **host-neutral abstract value model**: the
thing every decoder must produce from a given byte sequence, and the
thing conformance vectors assert against. It is *not* natural JSON —
plain JSON is lossy for this model (no `NaN`/`Inf`/`-0`, no Null vs
Undefined distinction, numbers lose f64/BigInt precision). So every
value is a **single-key tagged object** `{"<Variant>": <body>}`, and
anywhere JSON's native types would lose information we use a string.

This representation draws the conformance boundary: decoders must agree
down to *this* form. How a host then maps it to native types
(`str`/`dict`/`BigInt`/wrapper classes) is host-private and tested by
each adapter's own suite, never by the shared corpus.

| Variant | Debug-JSON | Notes |
|---------|-----------|-------|
| Null | `{"Null": null}` | distinct from Undefined |
| Undefined | `{"Undefined": null}` | distinct from Null |
| Bool | `{"Bool": true}` / `{"Bool": false}` | |
| Number (finite) | `{"Number": "0x3FF0000000000000"}` | **f64 as the 16-hex-digit big-endian bit pattern**, not a JSON number — the only lossless, unambiguous form (covers `-0`, subnormals, full precision). `+0.0` = `"0x0000000000000000"`, `-0.0` = `"0x8000000000000000"`. |
| Number (NaN) | `{"Number": "0x7FF8000000000000"}` | always exactly canonical quiet NaN; any other NaN pattern is a decode *rejection*, never appears here |
| Number (±Inf) | `{"Number": "0x7FF0000000000000"}` / `{"Number": "0xFFF0000000000000"}` | |
| BigInt | `{"BigInt": "-170141183460469231731687303715884105728"}` | canonical decimal string (no leading zeros, `-` only for negatives, `0` not `-0`) |
| String | `{"String": "hi"}` | JSON string; UTF-8 already validated at decode |
| Bytes | `{"Bytes": "00FF1A"}` | uppercase hex, no separators; empty = `{"Bytes": ""}` |
| Array | `{"Array": [ <value>, ... ]}` | ordered |
| Object | `{"Object": [ ["key", <value>], ... ]}` | **array of [key,value] pairs**, not a JSON object — preserves insertion order and permits the duplicate-key cases the corpus must express (a JSON object would silently dedup) |
| Handle | `{"Handle": {"context_id": 1, "handle_id": 5, "generation": 2}}` | the identity triple; small u32s so JSON numbers are exact |
| Error | `{"Error": {"name": "TypeError", "message": "x", "stack": "..."}}` | `stack` is `null` when absent (wire len 0) |

Rejections assert as `{"reject": "<reason_code>"}` against the
enumerated rejection-reason taxonomy (see Conformance obligations).

Decisions this representation forces (recorded here so they are not
rediscovered while writing vectors):

- **f64 is hex bits, not a JSON number.** A JSON number cannot represent
  `NaN`/`Inf`/`-0` and risks precision/rounding drift across three JSON
  libraries — fatal for an equality oracle. Hex bits are exact and
  unambiguous. *(Annotate if you'd rather a decimal form with explicit
  `NaN`/`Inf`/`-0` sentinels; I judge hex strictly safer.)*
- **Object is a pair-array, not a JSON object.** This preserves order
  and lets the corpus express duplicate-key inputs (a JSON object would
  collapse them, hiding the very case we need to test).
- **Bytes is hex, not a JSON array of ints** — compact and unambiguous.

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
request_id  : u64        # correlation; debugging + async settlement (u64: free-running counter, no wraparound mis-correlation)
kind        : u32        # which operation (eval, eval_handle, handle_get, ...)
flags       : u32        # per-kind orthogonal bitflags; reserved, must be 0
payload     : Value      # operation-specific, encoded per the value model
```

`request_id` is **u64** (decided 2026-06-13): it is a free-running
correlation counter, and a u32 could wrap on a long-lived multiplexed
instance, risking a stale response matched to a recycled id — on the
async-settlement path, resolving the wrong pending promise. u64 makes
that structurally impossible for 4 extra bytes. The other ids
(`handle_id`, `context_id`, `generation`) stay u32: they are
cap-bounded and generation/validation-protected, not free-running
counters with a correlation dependency.

`flags` carries *orthogonal, composable* per-kind options only (a future
example: eval strict-mode). Mutually-exclusive operation choices are
encoded as distinct `kind`s, not flags — e.g. result-mode is
`eval` vs `eval_handle` (and `eval_start` vs `eval_handle_start`), never
a flag bit. No flag bits are defined yet; **reserved bits must be zero**,
and any nonzero `flags` is rejected with `invalid_request` (canonical-form
rule). Keeping the field reserved lets the first orthogonal option claim
a bit without a wire-incompatible ABI bump.

Response payload rides the `AbiResponse` descriptor (below); the decoded
body is a `Value` (or an `Error` value when `status` indicates a JS
exception).

## AbiResponse descriptor

Fixed 16-byte struct written to guest memory / out-pointer:

```
status : u32     # coarse outcome (see Status codes)
tag    : u32     # payload-shape enum, interpreted relative to status
ptr    : u32     # guest linear-memory offset of the payload
len    : u32     # payload length in bytes
```

`tag` is a **pure payload-shape enum**, scoped per `status` (same value
means different shapes under different statuses — like request `flags`
are per-`kind`). Defined values today:

| status | tag | payload shape |
|--------|----:|---------------|
| `ok` | 0 | `value` — a value tree, no handle leaves, nothing to dispose |
| `ok` | 1 | `value_with_handles` — a value tree carrying handle leaves the host owns and must dispose (the disposal signal, modeled as a shape, not a flag bit) |
| `guest_error_response` | 0 | `GuestErrorRecord` |
| `guest_error_response` | 1 | `HostDiagnosticRecord` *(reserved slot; defined when diagnostics land)* |
| no-payload statuses (`invalid_*`, `timeout`, `stack_overflow`, …) | 0 | empty; `len = 0` |

Rules:

- An unknown `tag` for the given `status` is rejected (`invalid_request`
  / poison) — never best-effort. This canonical-form rule is what makes
  "enum now, split later" safe.
- **Enum now, split later (decided 2026-06-12).** `tag` stays a pure
  shape enum while handle-leaves is the only orthogonal response signal.
  If a *second* signal appears that must *combine* with handle-leaves
  (rather than be mutually exclusive with it), promote `tag` to an
  enum-plus-flags subfield split (low bits shape, high bits
  reserved-must-be-zero flags) — an ABI-versioned change at that point,
  not a silently-enabled bit. One signal = enum; two combinable = split.

Hosts validate `ptr + len` against linear memory with overflow checks
before reading (Host Adapter Security Requirements): `ptr`/`len` are
attacker-controlled under engine compromise — check the `u32` addition
does not wrap and `ptr + len <= memory size`, fail closed on either.
The descriptor's own 16-byte size is fixed and never guest-controlled,
so reading it is always safe; only what it points to needs validation.

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
12 deadlock               # eval can provably never progress (no pending host calls, jobs, or timers); distinct from timeout
```

`timeout`, `stack_overflow`, and `deadlock` are new vs the spec's
original list, added so hosts surface a precise condition rather than a
generic trap/panic. `timeout` (`TimeoutError`) and `stack_overflow`
(`StackOverflow`) replace what would otherwise be `guest_panic`.
`deadlock` is distinct from `timeout`: it is reported only when the eval
can *provably never* progress — no pending host calls, no scheduled
jobs, no timers — which the eval state machine already detects as its
`Deadlock` poll state. Mapping it to `timeout` would be lossy (timeout =
"ran too long, might have finished"; deadlock = "will never finish").
The guest must be certain (no pending anything) before reporting it; a
false deadlock report is worse than a timeout.

## Versioning rule

`quickjs-core-abi` is a versioned crate. `qrs_abi_version()` returns its
integer version. Any wire-incompatible change (new/removed/reordered tag,
changed field width, changed envelope) bumps the major ABI version;
additive feature-flagged changes bump minor. The guest and every host
fail fast (`abi_mismatch`) on a version they don't support — no
best-effort cross-version parsing.

## Rejection-reason taxonomy

Every decode rejection maps to one enumerated reason code, so conformance
vectors can assert *why* a malformed input is rejected (not merely "it
failed somehow" — which would let three decoders reject for three
different reasons and falsely pass). Every reason code must have at least
one corpus vector; a code with no vector means the corpus is incomplete.

```
unknown_value_tag        # tag byte not in 0x00..=0x0B
unknown_status           # AbiResponse status not in the defined set
unknown_response_tag     # tag invalid for the given status
non_canonical_nan        # NaN-class f64 that is not canonical quiet NaN
non_canonical_bigint     # leading zero, "-0", "+", non-digit, empty, non-decimal
invalid_utf8             # String/key/BigInt/Error field not valid UTF-8
length_exceeds_buffer    # a len/count larger than the remaining bytes
length_overflow          # ptr+len or offset+len wraps u32
depth_exceeded           # nesting past the depth cap
size_exceeded            # payload past the total-size cap
trailing_bytes           # bytes remain after a complete top-level value
truncated                # buffer ends mid-value
reserved_flag_set        # envelope flags has a nonzero (reserved) bit
duplicate_object_key     # same key appears twice in one Object (resolved: reject)
```

This list is part of the contract; adding a rejection condition adds a
reason code (and at least one vector), under the same ABI-versioning
discipline as the rest of the format.

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

1. ~~NaN canonicalization~~ **Resolved:** canonical quiet NaN
   `0x7FF8000000000000` on encode, reject other NaN-class patterns on
   decode; signed zero preserved. See Number notes above.
2. ~~Bool-in-tag vs body byte~~ **Resolved:** keep bool-in-tag
   (0x02/0x03) — smaller, no body to canonicalize.
3. ~~u32 integer fast-path~~ **Resolved:** f64-only, final for V1;
   revisit only if the marshalling benchmark shows it's a hotspot. See
   Number notes.
4. ~~`deadlock` status code~~ **Resolved:** added as status 12, distinct
   from `timeout`. See Status codes.
5. ~~`request_id` width~~ **Resolved:** u64 (only `request_id`; other ids
   stay u32). See envelope notes.

6. ~~Duplicate object keys~~ **Resolved (2026-06-13): reject** with
   `duplicate_object_key`. A correct encoder never emits duplicate keys,
   so receiving them is malformed/non-canonical input — fail closed
   rather than silently preserving or normalizing (consistent with the
   reject-non-canonical stance; refuses to transform adversarial input).
7. ~~Debug-JSON f64 form~~ **Resolved (2026-06-13): hex bit pattern.**
   The 16-hex-digit big-endian bits are exact, one-per-value, express
   NaN/Inf/-0 unambiguously, and compare identically across the three
   JSON libraries (a decimal form risks cross-library rounding — a false
   mismatch in an equality oracle). See Debug-JSON representation.

All open questions resolved.
