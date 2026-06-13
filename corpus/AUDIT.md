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
| S1.1a | R1 example "a length that could have been shorter" | Impossible in this format — lengths are fixed-width u32 (R2 forbids varints), so there is no shorter-length non-canonical form. Copy-paste from a varint mindset. Delete/replace the example. | 🔴 |
| S1.1b | R1 "reject with `invalid_request`" vs corpus `reject:<reason_code>` | The relationship between the `invalid_request` *status* and the taxonomy *reason codes* is never specified. How does a reason code ride the wire — payload of an `invalid_request` response? a field? The corpus asserts reason codes as first-class but the spec only names the status. **Foundational; later sections (AbiResponse) may depend on it.** | 🔴 |
| S1.1c | R1 "trailing bytes", "unknown tag" | Covered: `struct/reject-trailing-bytes`, `tag/reject-unknown-value-tag`, `tag/reject-unknown-value-tag-0C`. | ✓ |
| S1.2 | R2 little-endian fixed-width | Exercised by every multibyte vector; but unfalsifiable by a negative vector (any bytes are *some* valid LE value). Enforced by construction, not test. | ⚪ |
| S1.3 | R3 length-prefixed u32 | Covered by all String/Bytes/BigInt/Array/Object vectors. | ✓ |
| S1.4a | R4 length exceeds buffer | Covered: `string`/`bytes`/`array` reject-…-exceeds-buffer. | ✓ |
| S1.4b | R4 `offset+len` overflow (wrap) | Only covered at response level (`response/reject-ptr-len-overflow`). No **value-level** overflow vector (e.g. a String len that wraps offset+len). | 🟡 |
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
| S2.1 | Null / Undefined | Both tags present (distinction exercised). But neither appears as an **object value**, and Undefined appears **only top-level** (not in any container). | 🟡 |
| S2.2 | Bool | false/true, in arrays and object values. | ✓ |
| S2.3 | Number | Exhaustive: 0/-0/1/-1/1.5/±Inf/canonical-NaN/subnormal/largest-normal + 3 NaN rejects; top-level, array elem, object value. Reference-decoder verified. | ✓ |
| S2.4a | BigInt canonical-form | Rejects cover leading-zero / -0 / +sign / empty / non-digit; happy multi-digit covered by `beyond-u64`. | ✓ |
| S2.4b | BigInt position | Only ever top-level — never array elem or object value. | 🟡 |
| S2.5 | String | empty/ascii/2-3-4-byte UTF-8/embedded-NUL + 4 invalid-UTF-8 rejects + len-exceeds; top-level, array elem, object key. | ✓ |
| S2.6a | Bytes non-validation | `bytes/three` = `00FF1A` (FF is invalid UTF-8) proves Bytes does NOT UTF-8-validate — the key Bytes-vs-String distinction. | ✓ |
| S2.6b | Bytes position | Only top-level; never in a container. Low risk (same length-prefix path as String). | 🟡 |
| S2.7 | Array | empty/numbers/mixed/nested + count-exceeds + truncated-element. | ✓ |
| S2.8 | Object | empty/single/order-preserved/nested/handle-value + duplicate-key reject; bare-body key encoding now correct; order asserted. | ✓ |
| S2.9a | Handle | top / zero-generation OK; reject-truncated (8<12 bytes). Fixed-width, no length. | ✓ |
| S2.9b | Handle validity | Codec reads 12 bytes regardless of id values; handle *validity* (live? disposed? cross-context?) is explicitly NOT a codec concern — it's the handle-table's. No codec-level reject for absurd ids, by design. (Note in spec?) | 🟡 |
| S2.10a | Error | with-stack / absent-stack(len 0→null) cover the only conditional branch. | ✓ |
| S2.10b | Error invalid-UTF-8 | Taxonomy says `invalid_utf8` covers "Error field", but the only invalid_utf8 vectors are on String. Error-field UTF-8 validation is untested. | 🟡 |
| S2.10c | Error position | Never in a container (low risk — Error is typically a payload/top-level value). | ⚪ |

### Section 2 theme
The dominant gap is **uneven structural-position coverage**: Null,
Undefined, BigInt, Bytes, Error are tested in only one position, but the
README coverage criterion #1 requires every variant as top-level, array
elem, object value, and (where legal) object key. By the corpus's *own*
stated criterion it is incomplete. Resolution options: (a) add the
missing position vectors, or (b) weaken the criterion to "every variant
in ≥1 position plus containers tested generically" with rationale.
Decide during the resolution pass.

