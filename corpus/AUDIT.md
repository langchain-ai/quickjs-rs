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
