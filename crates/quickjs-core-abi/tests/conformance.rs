//! Conformance runner: drives the reference codec against the shared vector
//! suite (`conformance/abi/codec_vectors.jsonl`). Every host decoder must pass
//! the same file; this is the Rust side. For each vector:
//!   - `expect.ok`     -> decode(hex) must equal the debug-JSON value
//!   - `expect.reject` -> decode(hex) must fail with that reason code
//!
//! The debug-JSON -> Value parsing lives here (test-only), keeping the
//! serde_json dependency out of the shipped library.

use quickjs_core_abi::{decode_value, encode_value, ErrorRecord, Handle, Value};
use serde::Deserialize;
use serde_json::Value as J;
use std::path::PathBuf;

fn corpus_path() -> PathBuf {
    // CARGO_MANIFEST_DIR = crates/quickjs-core-abi ; suite is at repo-root.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../conformance/abi/codec_vectors.jsonl")
}

fn hex_upper(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02X}")).collect()
}

fn hex_to_bytes(h: &str) -> Vec<u8> {
    let h: String = h.chars().filter(|c| !c.is_whitespace()).collect();
    (0..h.len()).step_by(2).map(|i| u8::from_str_radix(&h[i..i + 2], 16).unwrap()).collect()
}

/// Parse a debug-JSON expected value into an abstract `Value`.
fn value_from_json(j: &J) -> Value {
    let obj = j.as_object().expect("variant object");
    assert_eq!(obj.len(), 1, "one variant key");
    let (variant, body) = obj.iter().next().unwrap();
    match variant.as_str() {
        "Null" => Value::Null,
        "Undefined" => Value::Undefined,
        "Bool" => Value::Bool(body.as_bool().unwrap()),
        "Number" => {
            let s = body.as_str().unwrap();
            let h = s.strip_prefix("0x").expect("0x prefix");
            assert_eq!(h.len(), 16, "16 hex digits");
            assert!(h.bytes().all(|b| b.is_ascii_digit() || (b'A'..=b'F').contains(&b)), "uppercase hex");
            Value::Number(u64::from_str_radix(h, 16).unwrap())
        }
        "BigInt" => Value::BigInt(body.as_str().unwrap().to_owned()),
        "String" => Value::String(body.as_str().unwrap().to_owned()),
        "Bytes" => {
            let s = body.as_str().unwrap();
            assert!(s.len() % 2 == 0 && s.bytes().all(|b| b.is_ascii_digit() || (b'A'..=b'F').contains(&b)), "uppercase hex pairs");
            Value::Bytes((0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect())
        }
        "Array" => Value::Array(body.as_array().unwrap().iter().map(value_from_json).collect()),
        "Object" => {
            let pairs = body.as_array().unwrap().iter().map(|p| {
                let p = p.as_array().unwrap();
                (p[0].as_str().unwrap().to_owned(), value_from_json(&p[1]))
            }).collect();
            Value::Object(pairs)
        }
        "Handle" => {
            let h = body.as_object().unwrap();
            let f = |k: &str| h[k].as_u64().unwrap() as u32;
            Value::Handle(Handle { context_id: f("context_id"), handle_id: f("handle_id"), generation: f("generation") })
        }
        "Error" => {
            let e = body.as_object().unwrap();
            Value::Error(ErrorRecord {
                name: e["name"].as_str().unwrap().to_owned(),
                message: e["message"].as_str().unwrap().to_owned(),
                stack: e["stack"].as_str().map(str::to_owned),
            })
        }
        other => panic!("unknown variant {other:?}"),
    }
}

#[test]
fn value_vectors_conform() {
    let text = std::fs::read_to_string(corpus_path()).expect("read vectors");
    let mut total = 0;
    let mut deferred = 0; // non-value vectors not yet exercised (envelope/response)
    let mut failures = Vec::new();

    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        // Disable serde_json's default recursion limit: the depth-128
        // vector's `expect` is a 128-deep nested JSON value (a fixture
        // detail, unrelated to the codec's own depth handling).
        let mut de = serde_json::Deserializer::from_str(line);
        de.disable_recursion_limit();
        let v: J = J::deserialize(&mut de).expect("parse vector line");
        // V1 covers value-kind vectors; envelope/response kinds are deferred
        // to their own codec layers. Count them explicitly rather than
        // silently skipping, so a dropped/mis-kinded vector can't pass unseen.
        if v["kind"].as_str() != Some("value") {
            deferred += 1;
            continue;
        }
        total += 1;
        let name = v["name"].as_str().unwrap();
        let bytes = hex_to_bytes(v["hex"].as_str().unwrap());
        let expect = &v["expect"];

        let got = decode_value(&bytes);
        if let Some(ok) = expect.get("ok") {
            match got {
                Ok(val) => {
                    let want = value_from_json(ok);
                    if val != want {
                        failures.push(format!("{name}: decoded {val:?}, expected {want:?}"));
                    } else {
                        // Bidirectional rule: OK vectors bind canonical encode
                        // too — encode(value) must reproduce the exact bytes.
                        match encode_value(&want) {
                            Ok(reencoded) if reencoded == bytes => {}
                            Ok(reencoded) => failures.push(format!(
                                "{name}: encode mismatch\n  got  {}\n  want {}",
                                hex_upper(&reencoded),
                                hex_upper(&bytes)
                            )),
                            Err(r) => failures.push(format!(
                                "{name}: encode rejected unexpectedly ({})",
                                r.as_str()
                            )),
                        }
                    }
                }
                Err(r) => failures.push(format!("{name}: expected ok, got reject {}", r.as_str())),
            }
        } else if let Some(reject) = expect.get("reject").and_then(J::as_str) {
            match got {
                Ok(val) => failures.push(format!("{name}: expected reject {reject}, decoded {val:?}")),
                Err(r) if r.as_str() != reject => {
                    failures.push(format!("{name}: expected reject {reject}, got {}", r.as_str()))
                }
                Err(_) => {}
            }
        }
    }

    // Pin the coverage explicitly: if the suite gains/loses value vectors,
    // this count must be updated deliberately — a silent skip can't inflate
    // or hide coverage. (67 value, 9 deferred envelope/response = 76 total.)
    assert_eq!(total, 67, "expected 67 value vectors, saw {total}");
    assert_eq!(deferred, 9, "expected 9 deferred (envelope/response) vectors, saw {deferred}");
    assert!(failures.is_empty(), "{} of {} value vectors failed:\n{}", failures.len(), total, failures.join("\n"));
    eprintln!("conformance: {total} value vectors passed; {deferred} envelope/response vectors deferred (not yet exercised)");
}
