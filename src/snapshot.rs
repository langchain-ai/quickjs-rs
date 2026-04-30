//! Snapshot envelope codec and metadata records.

use crate::ast::extract_top_level_declared_names;
use crate::context::QjsContext;
use crate::errors::QuickJSError;
use pyo3::exceptions::PyNotImplementedError;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rquickjs::qjs;
use serde::{Deserialize, Serialize};
use std::cell::{Cell, RefCell};
use std::collections::HashSet;
use std::ffi::CStr;

const MAGIC: &[u8; 4] = b"QJTM";
const FORMAT_VERSION: u8 = 1;
const SCHEMA_NAME: &str = "quickjs-rs.snapshot.v1";
const RQUICKJS_VERSION: &str = "0.11.0";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SnapshotHeader {
    pub(crate) schema: String,
    pub(crate) format_version: u8,
    pub(crate) quickjs_version: String,
    pub(crate) rquickjs_version: String,
    pub(crate) crate_version: String,
    pub(crate) allow_bytecode: bool,
    pub(crate) allow_reference: bool,
    pub(crate) allow_sab: bool,
    pub(crate) record_count: usize,
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct SnapshotFlags {
    pub(crate) allow_bytecode: bool,
    pub(crate) allow_reference: bool,
    pub(crate) allow_sab: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum SnapshotRecordKind {
    Active,
    TombstoneUnserializable,
    TombstoneMissingName,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SnapshotNameRecord {
    pub(crate) name: String,
    pub(crate) record_kind: SnapshotRecordKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) type_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) hint: Option<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct DecodedSnapshot {
    pub(crate) header: SnapshotHeader,
    pub(crate) values_blob: Vec<u8>,
    pub(crate) records: Vec<SnapshotNameRecord>,
}

pub(crate) struct SnapshotState {
    name_registry: RefCell<Vec<String>>,
    name_seen: RefCell<HashSet<String>>,
    module_touched: Cell<bool>,
}

pub(crate) struct SnapshotManager;

impl SnapshotState {
    pub(crate) fn new() -> Self {
        Self {
            name_registry: RefCell::new(Vec::new()),
            name_seen: RefCell::new(HashSet::new()),
            module_touched: Cell::new(false),
        }
    }

    pub(crate) fn debug_registry_names(&self) -> Vec<String> {
        self.name_registry.borrow().clone()
    }

    pub(crate) fn registry_names(&self) -> Vec<String> {
        self.name_registry.borrow().clone()
    }

    pub(crate) fn module_touched(&self) -> bool {
        self.module_touched.get()
    }

    pub(crate) fn track_eval(&self, code: &str, module: bool) {
        if module {
            self.module_touched.set(true);
        }
        let names = extract_top_level_declared_names(code, module);
        if module {
            return;
        }
        let Some(names) = names else {
            return;
        };
        self.merge_registry_names(&names);
    }

    pub(crate) fn merge_registry_names(&self, names: &[String]) {
        let mut seen = self.name_seen.borrow_mut();
        let mut registry = self.name_registry.borrow_mut();
        for name in names {
            if seen.insert(name.to_string()) {
                registry.push(name.to_string());
            }
        }
    }
}

impl SnapshotManager {
    pub(crate) fn create_snapshot(
        ctx: &QjsContext,
        state: &SnapshotState,
        on_unserializable: &str,
        on_missing_name: &str,
        flags: SnapshotFlags,
    ) -> PyResult<Vec<u8>> {
        if on_unserializable != "tombstone" && on_unserializable != "error" {
            return Err(PyValueError::new_err(
                "on_unserializable must be 'tombstone' or 'error'",
            ));
        }
        if on_missing_name != "skip" && on_missing_name != "tombstone" && on_missing_name != "error"
        {
            return Err(PyValueError::new_err(
                "on_missing_name must be 'skip', 'tombstone', or 'error'",
            ));
        }
        if state.module_touched() {
            return Err(PyNotImplementedError::new_err(
                "create_snapshot() is not implemented for contexts that \
executed module=True eval; module-mode snapshotting is not implemented",
            ));
        }
        if ctx.has_pending_snapshot_resolvers() {
            return Err(QuickJSError::new_err(
                "create_snapshot() cannot run while async host-call resolvers are pending",
            ));
        }

        let names = state.registry_names();
        let mut records: Vec<SnapshotNameRecord> = Vec::with_capacity(names.len());
        let mut active_values = Vec::with_capacity(names.len());

        for name in names {
            let handle = match ctx.snapshot_resolve_name_handle(name.as_str()) {
                Ok(h) => h,
                Err(err) => {
                    if on_missing_name == "error" {
                        return Err(err);
                    }
                    if on_missing_name == "tombstone" {
                        records.push(SnapshotNameRecord {
                            name: name.clone(),
                            record_kind: SnapshotRecordKind::TombstoneMissingName,
                            type_name: None,
                            hint: Some(missing_name_hint(name.as_str())),
                        });
                    }
                    continue;
                }
            };

            let persistent = handle.persistent_clone()?;
            let type_name = ctx.snapshot_handle_type(&handle)?;
            if ctx.snapshot_dump_handle_bytes(&handle, flags).is_err() {
                if on_unserializable == "error" {
                    return Err(QuickJSError::new_err(format!(
                        "value for '{}' is not serializable (type: {})",
                        name, type_name
                    )));
                }
                records.push(SnapshotNameRecord {
                    name: name.clone(),
                    record_kind: SnapshotRecordKind::TombstoneUnserializable,
                    type_name: Some(type_name.clone()),
                    hint: Some(unserializable_hint(name.as_str(), type_name.as_str())),
                });
                continue;
            }

            records.push(SnapshotNameRecord {
                name: name.clone(),
                record_kind: SnapshotRecordKind::Active,
                type_name: None,
                hint: None,
            });
            active_values.push((name, persistent));
        }

        let values_blob = ctx.snapshot_dump_active_values_blob(&active_values, flags)?;
        encode_snapshot(&values_blob, &records, flags)
    }

    pub(crate) fn restore_snapshot_bytes(
        ctx: &QjsContext,
        state: &SnapshotState,
        data: &[u8],
        inject_globals: bool,
    ) -> PyResult<()> {
        let decoded = decode_snapshot(data)?;
        let runtime_qjs = current_quickjs_version();
        if decoded.header.quickjs_version != runtime_qjs {
            return Err(PyValueError::new_err(format!(
                "snapshot QuickJS version {} does not match runtime QuickJS version {}",
                decoded.header.quickjs_version, runtime_qjs
            )));
        }

        let flags = SnapshotFlags {
            allow_bytecode: decoded.header.allow_bytecode,
            allow_reference: decoded.header.allow_reference,
            allow_sab: decoded.header.allow_sab,
        };
        let loaded = ctx.snapshot_load_handle_bytes(&decoded.values_blob, flags)?;
        if !inject_globals {
            return Ok(());
        }

        let injected_names = ctx.snapshot_inject_active_globals(&loaded, &decoded.records)?;
        ctx.snapshot_install_tombstones(&decoded.records)?;
        state.merge_registry_names(&injected_names);
        Ok(())
    }
}

pub(crate) fn current_quickjs_version() -> String {
    let raw = unsafe { qjs::JS_GetVersion() };
    if raw.is_null() {
        return "unknown".to_string();
    }
    unsafe { CStr::from_ptr(raw) }
        .to_string_lossy()
        .into_owned()
}

pub(crate) fn missing_name_hint(name: &str) -> String {
    format!(
        "Value for '{}' was not captured because the identifier was not resolvable after turn execution.",
        name
    )
}

pub(crate) fn unserializable_hint(name: &str, type_name: &str) -> String {
    format!(
        "Value for '{}' was not restored because it is not serializable (type: {}). Hint: this value is unavailable after snapshot restore.",
        name, type_name
    )
}

pub(crate) fn encode_snapshot(
    values_blob: &[u8],
    records: &[SnapshotNameRecord],
    flags: SnapshotFlags,
) -> PyResult<Vec<u8>> {
    let header = SnapshotHeader {
        schema: SCHEMA_NAME.to_string(),
        format_version: FORMAT_VERSION,
        quickjs_version: current_quickjs_version(),
        rquickjs_version: RQUICKJS_VERSION.to_string(),
        crate_version: env!("CARGO_PKG_VERSION").to_string(),
        allow_bytecode: flags.allow_bytecode,
        allow_reference: flags.allow_reference,
        allow_sab: flags.allow_sab,
        record_count: records.len(),
    };
    let header_json = serde_json::to_vec(&header)
        .map_err(|e| PyValueError::new_err(format!("snapshot header encode failed: {}", e)))?;
    let records_json = serde_json::to_vec(records)
        .map_err(|e| PyValueError::new_err(format!("snapshot metadata encode failed: {}", e)))?;

    let header_len =
        u32::try_from(header_json.len()).map_err(|_| PyValueError::new_err("header too large"))?;
    let values_len = u32::try_from(values_blob.len())
        .map_err(|_| PyValueError::new_err("values blob too large"))?;
    let records_len = u32::try_from(records_json.len())
        .map_err(|_| PyValueError::new_err("name metadata too large"))?;

    let mut out = Vec::with_capacity(
        4 + 1 + 4 + header_json.len() + 4 + values_blob.len() + 4 + records_json.len(),
    );
    out.extend_from_slice(MAGIC);
    out.push(FORMAT_VERSION);
    out.extend_from_slice(&header_len.to_le_bytes());
    out.extend_from_slice(&header_json);
    out.extend_from_slice(&values_len.to_le_bytes());
    out.extend_from_slice(values_blob);
    out.extend_from_slice(&records_len.to_le_bytes());
    out.extend_from_slice(&records_json);
    Ok(out)
}

pub(crate) fn decode_snapshot(data: &[u8]) -> PyResult<DecodedSnapshot> {
    if data.len() < 5 {
        return Err(PyValueError::new_err("snapshot payload is too short"));
    }
    if &data[..4] != MAGIC {
        return Err(PyValueError::new_err("invalid snapshot magic"));
    }
    let wire_version = data[4];
    if wire_version != FORMAT_VERSION {
        return Err(PyValueError::new_err(format!(
            "unsupported snapshot format version {}; expected {}",
            wire_version, FORMAT_VERSION
        )));
    }

    let mut cursor = 5usize;
    let header_len = read_u32(data, &mut cursor)? as usize;
    let header_bytes = read_bytes(data, &mut cursor, header_len)?;
    let header: SnapshotHeader = serde_json::from_slice(header_bytes)
        .map_err(|e| PyValueError::new_err(format!("invalid snapshot header JSON: {}", e)))?;
    if header.format_version != FORMAT_VERSION {
        return Err(PyValueError::new_err(format!(
            "unsupported snapshot header format version {}; expected {}",
            header.format_version, FORMAT_VERSION
        )));
    }
    if header.schema != SCHEMA_NAME {
        return Err(PyValueError::new_err(format!(
            "unsupported snapshot schema {:?}",
            header.schema
        )));
    }

    let values_len = read_u32(data, &mut cursor)? as usize;
    let values_blob = read_bytes(data, &mut cursor, values_len)?.to_vec();
    let records_len = read_u32(data, &mut cursor)? as usize;
    let records_bytes = read_bytes(data, &mut cursor, records_len)?;
    let records: Vec<SnapshotNameRecord> = serde_json::from_slice(records_bytes)
        .map_err(|e| PyValueError::new_err(format!("invalid snapshot metadata JSON: {}", e)))?;
    if records.len() != header.record_count {
        return Err(PyValueError::new_err(format!(
            "snapshot record_count mismatch: header={}, metadata={}",
            header.record_count,
            records.len()
        )));
    }
    if cursor != data.len() {
        return Err(PyValueError::new_err("snapshot payload has trailing bytes"));
    }

    Ok(DecodedSnapshot {
        header,
        values_blob,
        records,
    })
}

fn read_u32(data: &[u8], cursor: &mut usize) -> PyResult<u32> {
    let end = cursor.saturating_add(4);
    if end > data.len() {
        return Err(PyValueError::new_err("snapshot payload truncated"));
    }
    let bytes = [
        data[*cursor],
        data[*cursor + 1],
        data[*cursor + 2],
        data[*cursor + 3],
    ];
    *cursor = end;
    Ok(u32::from_le_bytes(bytes))
}

fn read_bytes<'a>(data: &'a [u8], cursor: &mut usize, len: usize) -> PyResult<&'a [u8]> {
    let end = cursor.saturating_add(len);
    if end > data.len() {
        return Err(PyValueError::new_err("snapshot payload truncated"));
    }
    let out = &data[*cursor..end];
    *cursor = end;
    Ok(out)
}
