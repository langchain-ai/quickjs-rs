/* quickjs-wasm C shim — public ABI.
 *
 * See spec/implementation.md §6 for semantics. The shim exposes the
 * QuickJS runtime/context/value API to the Python host through a
 * slot-id indirection so that raw JSValue structs never cross the
 * wasm boundary (§6.1, §6.4).
 *
 * Calling convention for exports that return int32_t status:
 *     0 = ok
 *     1 = JS exception raised (when out_slot is present, it holds the exception)
 *    <0 = shim error (OOM, invalid slot, marshaling failure, etc.)
 */
#ifndef QUICKJS_WASM_SHIM_H
#define QUICKJS_WASM_SHIM_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define QJS_EXPORT __attribute__((visibility("default")))

/* ---- Runtime lifecycle (§6.2) -------------------------------------- */

QJS_EXPORT uint32_t qjs_runtime_new(void);
QJS_EXPORT void     qjs_runtime_free(uint32_t rt);
QJS_EXPORT void     qjs_runtime_set_memory_limit(uint32_t rt, uint64_t bytes);
QJS_EXPORT void     qjs_runtime_set_stack_limit(uint32_t rt, uint64_t bytes);
QJS_EXPORT int32_t  qjs_runtime_run_pending_jobs(uint32_t rt, uint32_t *out_count);
QJS_EXPORT bool     qjs_runtime_has_pending_jobs(uint32_t rt);
QJS_EXPORT void     qjs_runtime_install_interrupt(uint32_t rt);

/* ---- Context lifecycle (§6.2) -------------------------------------- */

QJS_EXPORT uint32_t qjs_context_new(uint32_t rt);
QJS_EXPORT void     qjs_context_free(uint32_t ctx);

/* ---- Slot management (§6.1) ---------------------------------------- */

QJS_EXPORT uint32_t qjs_slot_dup(uint32_t ctx, uint32_t slot);
QJS_EXPORT void     qjs_slot_drop(uint32_t ctx, uint32_t slot);

/* ---- Eval (§6.2) --------------------------------------------------- */

/* flags: bit 0 = module, bit 1 = compile-only, bit 2 = strict. */
QJS_EXPORT int32_t qjs_eval(uint32_t ctx,
                            uint32_t code_ptr, uint32_t code_len,
                            uint32_t flags,
                            uint32_t *out_slot);

/* ---- Globals and properties (§6.2) --------------------------------- */

QJS_EXPORT int32_t qjs_get_global_object(uint32_t ctx, uint32_t *out_slot);
QJS_EXPORT int32_t qjs_get_prop(uint32_t ctx, uint32_t obj_slot,
                                uint32_t key_ptr, uint32_t key_len,
                                uint32_t *out_slot);
QJS_EXPORT int32_t qjs_set_prop(uint32_t ctx, uint32_t obj_slot,
                                uint32_t key_ptr, uint32_t key_len,
                                uint32_t val_slot);
QJS_EXPORT int32_t qjs_get_prop_u32(uint32_t ctx, uint32_t obj_slot,
                                    uint32_t index, uint32_t *out_slot);

/* ---- Function invocation (§6.2) ------------------------------------ */

QJS_EXPORT int32_t qjs_call(uint32_t ctx, uint32_t fn_slot, uint32_t this_slot,
                            uint32_t argc, uint32_t argv_ptr,
                            uint32_t *out_slot);
QJS_EXPORT int32_t qjs_new_instance(uint32_t ctx, uint32_t ctor_slot,
                                    uint32_t argc, uint32_t argv_ptr,
                                    uint32_t *out_slot);

/* ---- Marshaling (§6.2, §8) ----------------------------------------- */

/* qjs_to_msgpack writes into a per-context scratch buffer (§6.4). The
 * returned (out_ptr, out_len) are valid until the next marshaling call
 * on this context. */
QJS_EXPORT int32_t qjs_to_msgpack(uint32_t ctx, uint32_t slot,
                                  uint32_t *out_ptr, uint32_t *out_len);
QJS_EXPORT int32_t qjs_from_msgpack(uint32_t ctx,
                                    uint32_t data_ptr, uint32_t data_len,
                                    uint32_t *out_slot);
QJS_EXPORT int32_t qjs_exception_to_msgpack(uint32_t ctx, uint32_t exc_slot,
                                            uint32_t *out_ptr,
                                            uint32_t *out_len);

/* ---- Type inspection (§6.2) ---------------------------------------- */

/* Return values correspond to quickjs_wasm.handle.ValueKind (§7.2). */
QJS_EXPORT uint32_t qjs_type_of(uint32_t ctx, uint32_t slot);
QJS_EXPORT bool     qjs_is_promise(uint32_t ctx, uint32_t slot);
/* 0 = pending, 1 = fulfilled, 2 = rejected, -1 = not a promise */
QJS_EXPORT int32_t  qjs_promise_state(uint32_t ctx, uint32_t slot);

/* ---- Promise settlement and inspection (§6.2, v0.2) ---------------- */

/* Return the fulfillment value / rejection reason as a fresh slot.
 * Returns negative status on a pending promise — callers must check
 * qjs_promise_state first. */
QJS_EXPORT int32_t qjs_promise_result(uint32_t ctx, uint32_t promise_slot,
                                      uint32_t *out_slot);

/* Settle a pending promise keyed by the pending_id returned from
 * host_call_async. The value/reason is decoded from msgpack in guest
 * memory. Calling resolve or reject twice on the same pending_id, or
 * on an unknown one, returns negative status (no side effects).
 * §6.4 v0.2 invariant. */
QJS_EXPORT int32_t qjs_promise_resolve(uint32_t ctx, uint32_t pending_id,
                                       uint32_t value_msgpack_ptr,
                                       uint32_t value_msgpack_len);
QJS_EXPORT int32_t qjs_promise_reject(uint32_t ctx, uint32_t pending_id,
                                      uint32_t reason_msgpack_ptr,
                                      uint32_t reason_msgpack_len);

/* ---- Host functions (§6.2) ----------------------------------------- */

/* is_async: 0 = dispatches through host_call (sync), returns value directly.
 *           1 = dispatches through host_call_async (v0.2), creates a JS
 *               Promise and settles via qjs_promise_resolve/reject. */
QJS_EXPORT int32_t qjs_register_host_function(uint32_t ctx,
                                              uint32_t name_ptr,
                                              uint32_t name_len,
                                              uint32_t fn_id,
                                              uint32_t is_async);

/* ---- Guest memory (§6.2) ------------------------------------------- */

QJS_EXPORT uint32_t qjs_malloc(uint32_t size);
QJS_EXPORT void     qjs_free(uint32_t ptr);

/* ---- Imports (§6.3) ------------------------------------------------ */

/* Dispatched when JS calls a sync host-registered function. The host
 * writes its reply into a qjs_malloc-allocated guest buffer and hands
 * back the pointer/length; the shim qjs_free's it after consuming. */
__attribute__((import_name("host_call")))
int32_t host_call(uint32_t fn_id,
                  uint32_t args_ptr, uint32_t args_len,
                  uint32_t *out_ptr, uint32_t *out_len);

/* v0.2: Dispatched when JS calls an async host-registered function.
 * The host schedules the real work, returns an opaque pending_id in
 * *out_pending_id, and settles the promise later via
 * qjs_promise_resolve / qjs_promise_reject. Non-zero return means the
 * host synchronously rejected the call (no Promise is created, no
 * settlement expected). */
__attribute__((import_name("host_call_async")))
int32_t host_call_async(uint32_t fn_id,
                        uint32_t args_ptr, uint32_t args_len,
                        uint32_t *out_pending_id);

/* Non-zero return aborts the currently running JS. */
__attribute__((import_name("host_interrupt")))
int32_t host_interrupt(void);

#ifdef __cplusplus
}
#endif

#endif /* QUICKJS_WASM_SHIM_H */
