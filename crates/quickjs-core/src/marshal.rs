//! Marshal an rquickjs value into the shared ABI `Value`. Phase 1 covers the
//! by-value cases (primitives, string, bytes-as-array? no — array/object);
//! functions/promises/symbols and handles are deferred to later phases and
//! marshal as an error for now (no Handle table yet).

use quickjs_core_abi::{ErrorRecord, Value as AbiValue};
use rquickjs::{Ctx, Value as JsValue};

/// Convert a JS value to an ABI value, depth-bounded. Returns Err(message)
/// for cases not yet supported in Phase 1 (function, promise, symbol).
pub(crate) fn js_to_abi(ctx: &Ctx, v: &JsValue, depth: usize) -> Result<AbiValue, String> {
    if depth > quickjs_core_abi::limits::MAX_DEPTH {
        return Err("max marshal depth exceeded".to_string());
    }
    if v.is_null() {
        return Ok(AbiValue::Null);
    }
    if v.is_undefined() {
        return Ok(AbiValue::Undefined);
    }
    if let Some(b) = v.as_bool() {
        return Ok(AbiValue::Bool(b));
    }
    if let Some(i) = v.as_int() {
        return Ok(AbiValue::number(i as f64));
    }
    if let Some(f) = v.as_float() {
        return Ok(AbiValue::number(f));
    }
    if let Some(s) = v.as_string() {
        let s = s.to_string().map_err(|e| e.to_string())?;
        return Ok(AbiValue::String(s));
    }
    if let Some(arr) = v.as_array() {
        let mut items = Vec::new();
        for i in 0..arr.len() {
            let el: JsValue = arr.get(i).map_err(|e| e.to_string())?;
            items.push(js_to_abi(ctx, &el, depth + 1)?);
        }
        return Ok(AbiValue::Array(items));
    }
    if let Some(obj) = v.as_object() {
        // Function/promise are objects too; reject them in Phase 1 (Handle
        // table not built yet). A plain data object marshals key/value.
        if obj.as_function().is_some() {
            return Err("function values require a Handle (not in Phase 1)".to_string());
        }
        let mut pairs = Vec::new();
        for key in obj.keys::<String>() {
            let key = key.map_err(|e| e.to_string())?;
            let val: JsValue = obj.get(&key).map_err(|e| e.to_string())?;
            pairs.push((key, js_to_abi(ctx, &val, depth + 1)?));
        }
        return Ok(AbiValue::Object(pairs));
    }
    Err("unsupported value type for Phase 1 marshaling".to_string())
}

/// Build an ABI Error value from an rquickjs caught exception.
pub(crate) fn js_error_to_abi(ctx: &Ctx, err: rquickjs::Error) -> AbiValue {
    // For a thrown JS exception, pull the exception value off the ctx.
    if err.is_exception() {
        let exc = ctx.catch();
        if let Some(obj) = exc.as_object() {
            let name = obj.get::<_, String>("name").unwrap_or_else(|_| "Error".into());
            let message = obj.get::<_, String>("message").unwrap_or_default();
            let stack = obj.get::<_, String>("stack").ok().filter(|s| !s.is_empty());
            return AbiValue::Error(ErrorRecord { name, message, stack });
        }
        if let Some(s) = exc.as_string().and_then(|s| s.to_string().ok()) {
            return AbiValue::Error(ErrorRecord { name: "Error".into(), message: s, stack: None });
        }
    }
    AbiValue::Error(ErrorRecord { name: "Error".into(), message: err.to_string(), stack: None })
}
