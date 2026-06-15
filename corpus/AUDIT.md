# Codec Spec/Corpus Audit

A clause-by-clause audit of the wire codec spec
(`../docs/adr/0002-wire-codec.md`) against the conformance corpus
(`codec_vectors.jsonl`), in both directions:

- every normative spec clause → must have a vector proving it (or a noted
  reason it can't be tested), and
- every vector → must trace to a spec clause it exercises.

Anything in one without the other is a finding. The point is to break the
circularity of the corpus author also being the spec author: findings are
recorded as observations first, resolved second, so the resolution can be
checked against what was originally observed.

Status legend: 🔴 open bug · 🟡 open gap · 🟢 resolved · ✓ verified-ok · ⚪ n/a

---

## Section 1 — Design rules

| ID | Clause | Finding | Status |
|----|--------|---------|--------|
| S1.1a | R1 example "a length that could have been shorter" | Impossible in this format — lengths are fixed-width u32 (R2 forbids varints), so there is no shorter-length non-canonical form. **Resolved 2026-06-15:** replaced the example with real non-canonical forms (non-canonical NaN, non-canonical BigInt, trailing bytes, unknown tag) and added a note that fixed-width u32 means no shorter-length form exists. Also dropped the stray "with `invalid_request`" from the clause (down payment on S4.8). | 🟢 |
| S1.1b | R1 "reject with `invalid_request`" vs corpus `reject:<reason_code>` | **Resolved 2026-06-15 (with S4.8):** added a "Two failure concepts" subsection separating decode-failure *reasons* (taxonomy; decoder-side; host-local raise when the host decodes guest bytes) from operation *statuses* (guest-written wire values). The bridge: when the guest can't decode the host's request it writes `status=invalid_request` with the reason in `AbiResponse.tag` (enum, mutually exclusive — not a flags bit, per Hunter). Stray "with invalid_request" wording reworded. Corpus `{reject:<reason>}` confirmed to test direction-agnostic decoder behavior. | 🟢 |
| S1.1c | R1 "trailing bytes", "unknown tag" | Covered: `struct/reject-trailing-bytes`, `tag/reject-unknown-value-tag`, `tag/reject-unknown-value-tag-0C`. | ✓ |
| S1.2 | R2 little-endian fixed-width | Exercised by every multibyte vector; but unfalsifiable by a negative vector (any bytes are *some* valid LE value). Enforced by construction, not test. | ⚪ |
| S1.3 | R3 length-prefixed u32 | Covered by all String/Bytes/BigInt/Array/Object vectors. | ✓ |
| S1.4a | R4 length exceeds buffer | Covered: `string`/`bytes`/`array` reject-…-exceeds-buffer. | ✓ |
| S1.4b | R4 `offset+len` overflow (wrap) | **Resolved 2026-06-15:** the value-level overflow (small `len` at a huge `offset` so `offset+len` wraps u32) is **unreachable within the caps** — for a field to start at an offset near `0xFFFFFFFF` the buffer itself would have to be ~4 GiB, but the max payload cap is 32 MiB, so `offset+len` (both ≤32 MiB) cannot wrap at the value level. The decoder requirement is **retained unconditionally** (one-line `checked_add`; cheap insurance, and it becomes reachable if a cap is ever raised), but the dedicated value-level **vector is deferred to fuzzing** rather than the curated corpus, since constructing a realistic value-level wrap would require an artificial >4 GiB buffer the real system never produces. The response-level overflow vector (`response/reject-ptr-len-overflow`) stays. Format max string len = 4 GiB (u32); policy max = 32 MiB (cap). | 🟢 |
| S1.5 | R5 depth bound, depth-counted | Covered both sides: `depth/at-cap-128`, `depth/reject-over-cap-129`; plus `size_exceeded`. | ✓ |
| S1.6 | R6 debug-JSON is assertion target | Testing-infra statement, not a wire rule. No vector needed. | ⚪ |

### Section 1 open items to resolve
- **S1.1b (reason-code wire mechanism)** is the most important finding so
  far — resolve before or during the AbiResponse section.
- S1.1a: delete the impossible example.
- S1.4b: add a value-level overflow vector.

---

## Section 2 — Value model tag table

Per-variant: wire body well-defined? OK vector? rejects? structural
positions covered (top-level / array elem / object value / object key)?

| ID | Variant | Finding | Status |
|----|---------|---------|--------|
| S2.1 | Null / Undefined | Both tags present (distinction exercised). **Resolved 2026-06-15:** the format is uniformly recursive so position is a per-mechanism, not per-variant, concern; added the one genuinely-distinct case (body-less variant nested: `object/null-as-value`, `array/bodyless-elems`) and relaxed criterion #1 accordingly (see S2 theme). | 🟢 |
| S2.2 | Bool | false/true, in arrays and object values. | ✓ |
| S2.3 | Number | Exhaustive: 0/-0/1/-1/1.5/±Inf/canonical-NaN/subnormal/largest-normal + 3 NaN rejects; top-level, array elem, object value. Reference-decoder verified. | ✓ |
| S2.4a | BigInt canonical-form | Rejects cover leading-zero / -0 / +sign / empty / non-digit; happy multi-digit covered by `beyond-u64`. | ✓ |
| S2.4b | BigInt position | Only ever top-level. **Resolved 2026-06-15 (subsumed by S2 theme):** uniform recursion + relaxed criterion #1; bodied-variant nesting is exercised by `object/single-pair` (Number value), `object/handle-as-value`, nested arrays/objects. | 🟢 |
| S2.5 | String | empty/ascii/2-3-4-byte UTF-8/embedded-NUL + 4 invalid-UTF-8 rejects + len-exceeds; top-level, array elem, object key. | ✓ |
| S2.6a | Bytes non-validation | `bytes/three` = `00FF1A` (FF is invalid UTF-8) proves Bytes does NOT UTF-8-validate — the key Bytes-vs-String distinction. | ✓ |
| S2.6b | Bytes position | Only top-level. **Resolved 2026-06-15 (subsumed by S2 theme):** same length-prefix path as String (which is covered in containers), uniform recursion + relaxed criterion #1. | 🟢 |
| S2.7 | Array | empty/numbers/mixed/nested + count-exceeds + truncated-element. | ✓ |
| S2.8 | Object | empty/single/order-preserved/nested/handle-value + duplicate-key reject; bare-body key encoding now correct; order asserted. | ✓ |
| S2.9a | Handle | top / zero-generation OK; reject-truncated (8<12 bytes). Fixed-width, no length. | ✓ |
| S2.9b | Handle validity | Codec reads 12 bytes regardless of id values; handle *validity* (live? disposed? cross-context?) is explicitly NOT a codec concern — it's the handle-table's. No codec-level reject for absurd ids, by design. (Note in spec?) | 🟡 |
| S2.10a | Error | with-stack / absent-stack(len 0→null) cover the only conditional branch. | ✓ |
| S2.10b | Error invalid-UTF-8 | Taxonomy says `invalid_utf8` covers "Error field", but the only invalid_utf8 vectors are on String. Error-field UTF-8 validation is untested. | 🟡 |
| S2.10c | Error position | Never in a container (low risk — Error is typically a payload/top-level value). | ⚪ |

### Section 2 theme — RESOLVED 2026-06-15
The "uneven structural-position coverage" finding was correct against the
*old* criterion #1, but that criterion was over-specified for a uniformly
recursive format: a container holds "any encoded value" by the same code
path regardless of variant, so all-variants-in-all-positions is ~40
near-identical cells. **Resolution:** relaxed criterion #1 (README) to
"every variant top-level; containers exercised with a representative mix,
plus the one genuinely-distinct nested case — a *body-less* variant
nested — covered explicitly," and added `object/null-as-value` +
`array/bodyless-elems`. Broader nested coverage is the differential
fuzzer's job. This closes S2.1, S2.4b, S2.6b. (S2.10b Error-field
invalid-UTF-8 and S2.9b handle-validity-note remain separately.)

---

## Section 3 — Debug-JSON representation (the assertion target)

Meta-section: if the assertion target is ambiguous, every vector inherits
it. Standard applied: "could two *correct* decoders emit different JSON
for the same value?"

| ID | Row | Finding | Status |
|----|-----|---------|--------|
| S3.1 | Comparison semantics | **Resolved 2026-06-15:** added "Comparison semantics (structural)" subsection — parse both as JSON, compare as values, never byte-compare text. Three sub-rules pinned: JSON arrays order-sensitive (load-bearing for Array element + Object pair-array key order; with a "don't simplify Object to a JSON map" warning), JSON objects order-insensitive (wrapper, Handle/Error maps), string values exact-codepoint (which mandates S3.2). | 🟢 |
| S3.2 | Number / Bytes hex format | **Resolved 2026-06-15:** pinned canonical hex — Number: `0x` + uppercase + exactly 16 big-endian digits; Bytes: uppercase, no prefix, 2 digits/byte, no separators. Verified all existing Number/Bytes vectors already conform. Coupled to S3.1 (structural compare treats hex as case-sensitive strings). | 🟢 |
| S3.3 | String | **Resolved (subsumed by S3.1):** structural comparison neutralizes JSON escaping differences. | 🟢 |
| S3.4 | Object pair-array | **Resolved (S3.1):** JSON arrays compare order-sensitively, so pair-array key order is semantic (as `object/order-preserved` requires). | 🟢 |
| S3.5 | Handle / Error JSON objects | **Resolved (S3.1):** JSON objects compare order-insensitively, so field order is irrelevant. | 🟢 |
| S3.6 | Null/Undefined/Bool/Array/BigInt | One unambiguous form each. | ✓ |

### Section 3 theme
Same bug *shape* as S1.1a: a representation chosen "for being
unambiguous" whose own canonical form was never specified. Two real bugs:
**S3.1** (comparison must be defined as structural) and **S3.2** (hex
canonical form: uppercase, `0x`, fixed-width). Once S3.1 is pinned as
structural + S3.2 pins the hex form, the rest collapse to verified-ok.

---

## Section 4 — Limits, Envelope, AbiResponse

| ID | Clause | Finding | Status |
|----|--------|---------|--------|
| S4.1 | Limits: depth / count-exceeds / never-allocate-on-untrusted-len | depth both sides ✓; `array/reject-count-exceeds-buffer` ✓; "don't allocate before validating" is an impl obligation a vector can't prove (only fuzz/memory-profiling can). | ✓/⚪ |
| S4.2 | Limits: max total payload (32 MiB resp / 8 MiB arg) | The cap is **directional** (response vs host-call arg) and is a decoder-*configuration* parameter, not intrinsic to the bytes. A `value`-kind `size_exceeded` vector can't say which cap to apply. Vector format needs a per-vector `cap` parameter, or size-limit testing moves to per-host config tests. | 🟡 |
| S4.3 | Envelope `abi_version` → `abi_mismatch` | No vector for an unsupported abi_version. Versioning rule says fail fast; untested. | 🟡 |
| S4.4 | Envelope `request_id` u64 | `envelope/request-id-beyond-u32` (2^32) proves u64 width. | ✓ |
| S4.5 | Envelope `flags` reserved-must-be-zero | `envelope/reject-reserved-flag`. | ✓ |
| S4.6 | Envelope unknown `kind` | No vector; and status `unsupported` (6) has no vector anywhere. | 🟡 |
| S4.7 | **S1.1b RESOLVED: reject reasons are host-side decode classifications, not wire statuses** | Most taxonomy reasons (`unknown_value_tag`, `non_canonical_nan`, `length_overflow`…) are raised by *whichever decoder reads the bytes* (host reading guest output, or guest reading host input) — they are local decode failures, NOT an AbiResponse. The corpus `{"reject":<reason>}` tests **decoder behavior**, which is direction-agnostic and correct. The wire-status `invalid_request` is the guest-side *mirror*: what the guest writes when *it* can't decode the host's request. | 🟢 (resolves S1.1b) |
| S4.8 | **Spec conflates two kinds of "reject"** | **Resolved 2026-06-15:** added the "Two failure concepts" subsection (decode-failure reasons vs operation statuses), wired decode-reason into `AbiResponse.tag` under `invalid_request` (enum, not flags — Hunter's call, corrected from payload to tag), reworded stray `invalid_request`s in Rule 1 and the unknown-tag rule, and pointed the taxonomy intro at the distinction. Also added the bidirectional-encode conformance rule (spec + README): OK vectors bind decode AND canonical-encode. | 🟢 |

| S4.9 | New vector gap from the S4.8 resolution | Wiring decode-reason into `AbiResponse.tag` under `invalid_request` created a new testable shape — a guest-written `invalid_request` response carrying a reason in `tag` (e.g. `{status:2, tag:<unknown_value_tag enum>}`). No vector exercises it yet. Add in the resolution/vector pass. | 🟡 |

### Section 4 theme
The AbiResponse audit cracked S1.1b: **reject reasons are decoder
classifications, not wire statuses** — the corpus is testing the right
thing (direction-agnostic decoder behavior). But it exposed S4.8: the
spec muddles "decode-failure reason" and "operation status," which is the
*root* of the S1.1b confusion. Resolving S4.8 (clearly separate the two,
and state that the taxonomy is decoder-side) will retroactively clean up
S1.1b. Smaller gaps: directional size cap needs a vector mechanism (S4.2),
abi_mismatch / unknown-kind / `unsupported` untested (S4.3, S4.6).

