"""Emits the corpus vectors whose bytes are mechanical to compute (deep
nesting) or structurally two-part (AbiResponse = 16-byte descriptor +
payload). Hand-writing these is error-prone; generating them keeps the
bytes exact and reviewable. Run to regenerate the GENERATED block of
codec_vectors.jsonl; the hand-authored vectors above that block are not
touched.

Response vectors use a `descriptor` + `payload` split in the JSON (the
host harness concatenates: 16-byte descriptor, then payload bytes at the
offset the descriptor's ptr names — here always immediately following).

  python corpus/gen_generated_vectors.py > corpus/_generated.jsonl
"""
import json
import struct


def u32(n):
    return struct.pack('<I', n).hex().upper()


def line(obj):
    return json.dumps(obj, ensure_ascii=False)


vectors = []

# --- deep nesting: Array tag 0x08, count 1, repeated; innermost Null 0x00 ---
def nested_array_bytes(depth):
    # depth Arrays each of count 1, innermost value Null
    return ('08' + u32(1)) * depth + '00'

def nested_array_expect(depth):
    v = {"Null": None}
    for _ in range(depth):
        v = {"Array": [v]}
    return v

DEPTH_CAP = 128
vectors.append({"name": "depth/at-cap-128", "kind": "value",
                "hex": nested_array_bytes(DEPTH_CAP),
                "expect": {"ok": nested_array_expect(DEPTH_CAP)}})
vectors.append({"name": "depth/reject-over-cap-129", "kind": "value",
                "hex": nested_array_bytes(DEPTH_CAP + 1),
                "expect": {"reject": "depth_exceeded"}})

# --- AbiResponse: status(u32) tag(u32) ptr(u32) len(u32) + payload ---
# ptr is the guest offset of the payload; in these vectors the harness
# places the payload right after the descriptor, so ptr=16.
def response(name, status, tag, payload_hex, expect):
    payload = payload_hex.replace(' ', '')
    desc = u32(status) + u32(tag) + u32(16) + u32(len(payload) // 2)
    vectors.append({"name": name, "kind": "response",
                    "descriptor": desc, "payload": payload, "expect": expect})

response("response/ok-value", 0, 0, "06 01000000 78",
         {"ok": {"status": 0, "tag": 0, "payload": {"String": "x"}}})
response("response/ok-value-with-handles", 0, 1, "0A 01000000 05000000 02000000",
         {"ok": {"status": 0, "tag": 1,
                 "payload": {"Handle": {"context_id": 1, "handle_id": 5, "generation": 2}}}})
# guest error: Error{name="TypeError", message="boom", stack absent}
err = ("0B"
       + u32(9) + "TypeError".encode().hex().upper()
       + u32(4) + "boom".encode().hex().upper()
       + u32(0))
response("response/guest-error", 1, 0, err,
         {"ok": {"status": 1, "tag": 0,
                 "payload": {"Error": {"name": "TypeError", "message": "boom", "stack": None}}}})

for v in vectors:
    print(line(v))
