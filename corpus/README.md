# Codec Conformance Corpus

`codec_vectors.jsonl` is the **single standard test set every host decoder
must pass.** One file, language-neutral. Rust, Python, and TypeScript host
codecs each run their own decoder against every vector and must agree with
it. It is the executable form of the wire codec spec
(`../docs/adr/0002-wire-codec.md`) and the thing that holds three
independently-written decoders to one behavior.

## The contract

- **Every host passes every vector.** No per-host subset, no per-host
  variant. A vector a host cannot pass is a bug in that host's decoder
  (or, if all hosts fail it identically, a bug in the vector — the bytes
  are hand-derived from the spec, so the spec is the authority).
- **OK vectors are bidirectional.** Each `{"ok": value}` vector binds
  both `decode(hex) == value` *and* `encode(value) == hex` — the corpus
  bytes are *the* canonical encoding, so a conformant encoder must
  reproduce them exactly. Reject vectors are decode-only (an invalid
  value cannot be encoded). This gives encoder conformance for free from
  the same file.
- **Decode/encode is asserted in the debug-JSON abstract value model**
  (spec → Debug-JSON representation), not in native types. A host's test
  harness decodes the bytes into that host-neutral tagged form (and
  encodes from it) and compares to `expect`. This is what lets one file
  serve all three languages. Comparison is **structural value equality**
  (equal variant, recursively equal contents — not a textual compare of
  any serialized form): ordered contents (Array elements, Object keys)
  order-sensitive, field sets order-insensitive, strings exact-codepoint.
  A harness may compare via the debug-JSON serialization or via native
  decoded values; JSON is the corpus's notation, not the comparison
  mechanism. See the spec's "Comparison semantics" section.
- **Native-type mapping is out of scope.** Whether a decoded `{"String":
  "hi"}` becomes a Python `str`, a JS `string`, or a Rust `String`, and
  whether an `{"Object": ...}` becomes a dict or a wrapper, is each
  adapter's own concern, tested by that adapter's own suite — never here.
  The corpus stops at bytes ↔ abstract value.
- **Rejections assert a reason code**, not just "failed". A decoder must
  reject the input *and* for the enumerated reason (spec →
  Rejection-reason taxonomy). Three decoders rejecting for three
  different reasons is a divergence, not a pass.

This corpus is the curated **ground-truth oracle**. It complements, and
does not replace, the spec's two other obligations: differential fuzzing
(random inputs, decoders must agree — catches disagreements but not
"all wrong the same way") and round-trip testing (`decode(encode(v)) ==
v`). The corpus is the only one of the three that pins *correctness*
against the spec.

## Vector format

One JSON object per line:

```json
{"name":"string/ascii","kind":"value","hex":"06 02000000 6869","expect":{"ok":{"String":"hi"}}}
{"name":"nan/non-canonical","kind":"value","hex":"04 010000000000F87F","expect":{"reject":"non_canonical_nan"}}
```

- `name` — stable id, `category/case`. Human-meaning of the bytes lives
  here (the assertion target is exact, not readable).
- `kind` — `value` (a single encoded value), `envelope` (a request
  envelope), or `response` (an `AbiResponse` descriptor + payload).
- `hex` — the literal wire bytes, space-separated for readability;
  strip spaces before decoding. Multi-byte integers are
  **little-endian** per the spec.
- `expect` — `{"ok": <debug-JSON value>}` or `{"reject": "<reason_code>"}`.

## Debug-JSON quick reference

(authoritative form in the spec; summarized here for reading the vectors)

| Variant | Form |
|---|---|
| Null / Undefined | `{"Null":null}` / `{"Undefined":null}` |
| Bool | `{"Bool":true}` |
| Number | `{"Number":"0x<16 hex, big-endian f64 bits>"}` |
| BigInt | `{"BigInt":"<canonical decimal>"}` |
| String | `{"String":"..."}` |
| Bytes | `{"Bytes":"<uppercase hex>"}` |
| Array | `{"Array":[ ... ]}` |
| Object | `{"Object":[["k", <value>], ...]}` (ordered pair-array) |
| Handle | `{"Handle":{"context_id":N,"handle_id":N,"generation":N}}` |
| Error | `{"Error":{"name":"..","message":"..","stack":".."|null}}` |

Note `Number` is the **big-endian** bit pattern for readability (most
significant byte first), even though the wire encodes f64 little-endian.
The harness converts; the `hex` field is always little-endian wire bytes.

## Coverage criterion

The corpus claims exhaustiveness by construction, not by taste. It is
generated from a matrix and is complete only when:

1. **Every value variant** appears at least **top-level**. Container
   positions (array element, object value) are exercised with a
   representative mix rather than exhaustively per variant — the format
   is uniformly recursive (a container holds "any encoded value" by the
   same code path regardless of variant), so placing all variants in all
   positions would be ~40 near-identical cells. The one genuinely
   distinct nested case — a **body-less variant** (Null/Undefined, which
   have no body, unlike bodied variants like Handle) decoded in a nested
   position — is covered explicitly (`object/null-as-value`,
   `array/bodyless-elems`); broader nested coverage is left to the
   differential fuzzer, which hammers nested positions far harder than a
   hand matrix. String-as-object-key is covered (every object vector).
2. **Every per-type boundary** has a vector: string empty/1-byte/multibyte
   UTF-8/embedded NUL; f64 ±0, ±Inf, canonical NaN, subnormal, normal
   extremes; BigInt 0 / negative / beyond u64 / huge; empty containers;
   depth at cap and cap+1.
3. **Every rejection reason code** (spec taxonomy) has ≥1 vector. A code
   with no vector means the corpus is incomplete — this is the mechanical
   completeness check.
4. **Cross-cutting**: cycle → existing-handle leaf; shared-acyclic →
   duplicated; envelope round-trip incl. u64 `request_id`; `AbiResponse`
   `value` / `value_with_handles` / error shapes.

`coverage.md` (future) will track the matrix cells against vector names.
