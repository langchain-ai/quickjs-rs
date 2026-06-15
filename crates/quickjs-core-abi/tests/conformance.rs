//! Conformance runner: drives the reference codec against the shared vector
//! suite (`conformance/abi/codec_vectors.jsonl`). Every host decoder must pass
//! the same file; this is the Rust side. For each vector:
//!   - `expect.ok`     -> decode(hex) must equal the debug-JSON value
//!   - `expect.reject` -> decode(hex) must fail with that reason code
//!
//! The debug-JSON -> Value parsing lives here (test-only), keeping the
//! serde_json dependency out of the shipped library.

use quickjs_core_abi::{
    decode_envelope, decode_response, decode_value, encode_envelope, encode_value, Envelope,
    ErrorRecord, Handle, Value,
};
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

fn check_value(name: &str, bytes: &[u8], expect: &J, failures: &mut Vec<String>) {
    let got = decode_value(bytes);
    if let Some(ok) = expect.get("ok") {
        match got {
            Ok(val) => {
                let want = value_from_json(ok);
                if val != want {
                    failures.push(format!("{name}: decoded {val:?}, expected {want:?}"));
                } else {
                    // Bidirectional: OK vectors bind canonical encode too.
                    match encode_value(&want) {
                        Ok(re) if re == bytes => {}
                        Ok(re) => failures.push(format!(
                            "{name}: encode mismatch\n  got  {}\n  want {}",
                            hex_upper(&re),
                            hex_upper(bytes)
                        )),
                        Err(r) => failures.push(format!("{name}: encode rejected ({})", r.as_str())),
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

fn check_envelope(name: &str, bytes: &[u8], expect: &J, failures: &mut Vec<String>) {
    let got = decode_envelope(bytes);
    if let Some(ok) = expect.get("ok") {
        match got {
            Ok(env) => {
                let want = Envelope {
                    abi_version: ok["abi_version"].as_u64().unwrap() as u32,
                    request_id: ok["request_id"].as_u64().unwrap(),
                    kind: ok["kind"].as_u64().unwrap() as u32,
                    flags: ok["flags"].as_u64().unwrap() as u32,
                    payload: value_from_json(&ok["payload"]),
                };
                if env != want {
                    failures.push(format!("{name}: envelope decoded {env:?}, expected {want:?}"));
                } else {
                    match encode_envelope(&want) {
                        Ok(re) if re == bytes => {}
                        Ok(re) => failures.push(format!(
                            "{name}: envelope encode mismatch\n  got  {}\n  want {}",
                            hex_upper(&re),
                            hex_upper(bytes)
                        )),
                        Err(r) => failures.push(format!("{name}: envelope encode rejected ({})", r.as_str())),
                    }
                }
            }
            Err(r) => failures.push(format!("{name}: expected ok, got reject {}", r.as_str())),
        }
    } else if let Some(reject) = expect.get("reject").and_then(J::as_str) {
        match got {
            Ok(env) => failures.push(format!("{name}: expected reject {reject}, decoded {env:?}")),
            Err(r) if r.as_str() != reject => {
                failures.push(format!("{name}: expected reject {reject}, got {}", r.as_str()))
            }
            Err(_) => {}
        }
    }
}

fn check_response(name: &str, bytes: &[u8], expect: &J, failures: &mut Vec<String>) {
    let got = decode_response(bytes);
    if let Some(ok) = expect.get("ok") {
        match got {
            Ok(resp) => {
                let want_status = ok["status"].as_u64().unwrap() as u32;
                let want_tag = ok["tag"].as_u64().unwrap() as u32;
                let want_payload = value_from_json(&ok["payload"]);
                if resp.status as u32 != want_status || resp.tag != want_tag {
                    failures.push(format!(
                        "{name}: response status/tag {}/{} expected {}/{}",
                        resp.status as u32, resp.tag, want_status, want_tag
                    ));
                } else if want_payload != Value::Null && resp.payload != want_payload {
                    // Null-payload statuses carry no real payload (sentinel).
                    failures.push(format!("{name}: response payload {:?}, expected {:?}", resp.payload, want_payload));
                }
            }
            Err(r) => failures.push(format!("{name}: expected ok, got reject {}", r.as_str())),
        }
    } else if let Some(reject) = expect.get("reject").and_then(J::as_str) {
        match got {
            Ok(resp) => failures.push(format!("{name}: expected reject {reject}, decoded {resp:?}")),
            Err(r) if r.as_str() != reject => {
                failures.push(format!("{name}: expected reject {reject}, got {}", r.as_str()))
            }
            Err(_) => {}
        }
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
        let name = v["name"].as_str().unwrap();
        let expect = &v["expect"];
        match v["kind"].as_str() {
            Some("value") => {
                total += 1;
                check_value(name, &hex_to_bytes(v["hex"].as_str().unwrap()), expect, &mut failures);
            }
            Some("envelope") => {
                total += 1;
                check_envelope(name, &hex_to_bytes(v["hex"].as_str().unwrap()), expect, &mut failures);
            }
            Some("response") => {
                total += 1;
                // response vectors split descriptor + payload; concatenate.
                let mut bytes = hex_to_bytes(v["descriptor"].as_str().unwrap());
                bytes.extend_from_slice(&hex_to_bytes(v["payload"].as_str().unwrap_or("")));
                check_response(name, &bytes, expect, &mut failures);
            }
            other => {
                deferred += 1;
                failures.push(format!("{name}: unknown vector kind {other:?}"));
            }
        }
    }

    // Pin coverage: every vector is now exercised (no silent skip, no
    // deferral). 76 total = 67 value + 3 envelope + 6 response.
    assert_eq!(deferred, 0, "no vectors should be deferred now; saw {deferred}");
    assert_eq!(total, 76, "expected 76 exercised vectors, saw {total}");
    assert!(failures.is_empty(), "{} of {} vectors failed:\n{}", failures.len(), total, failures.join("\n"));
    eprintln!("conformance: all {total} vectors passed (value + envelope + response)");
}
