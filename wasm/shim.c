/* quickjs-wasm C shim. See spec/implementation.md §6.
 *
 * Current state: minimum implementation to green the first assertion in
 * tests/test_smoke.py (ctx.eval("1 + 2") == 3). Additional exports are
 * declared per §6.2 but return -1 (shim error) until their turn comes.
 *
 * Invariants (§6.4):
 *   - All slot-ID-taking exports validate the slot; never crash on bad input.
 *   - Per-context msgpack scratch buffer is invalidated on every marshaling
 *     call; the host must drain it before the next call.
 *   - JSValue structs never cross the wasm boundary; callers use slot IDs.
 */

#include "shim.h"
#include "quickjs.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* Slot table (§6.1)                                                   */
/* ------------------------------------------------------------------ */

/* Per-context state. Slot 0 is reserved as "invalid / null sentinel" so
 * that a zero return from export functions that produce slot IDs is a
 * safe error value. The slot table grows on demand.
 *
 * Lifetime: a live slot owns one refcount on its JSValue. qjs_slot_dup
 * bumps both the slot refcount and the JSValue refcount; qjs_slot_drop
 * decrements the slot refcount and, when it hits zero, releases the
 * JSValue and returns the slot to the free list.
 */

#define SLOT_INITIAL_CAP 64
#define SCRATCH_INITIAL_CAP (64 * 1024)
#define MAX_SLOTS_PER_CONTEXT (1u << 20) /* §9: 1M slots */

typedef struct Slot {
    JSValue value;
    uint32_t refcount; /* 0 = free slot */
    uint32_t next_free; /* free-list pointer when refcount == 0 */
} Slot;

typedef struct ShimContext {
    JSContext *ctx;
    Slot *slots;
    uint32_t slot_cap;
    uint32_t slot_count; /* high-water mark of allocated slots */
    uint32_t free_head; /* 0 means empty */
    uint8_t *scratch;
    uint32_t scratch_cap;
    uint32_t scratch_len;
    bool alive;
} ShimContext;

/* The shim supports multiple contexts, but v0.1 only needs a handful.
 * Contexts are addressed by small integer IDs so they fit in uint32_t.
 */
#define MAX_CONTEXTS 64
static ShimContext g_contexts[MAX_CONTEXTS];

static ShimContext *ctx_lookup(uint32_t ctx_id) {
    if (ctx_id == 0 || ctx_id > MAX_CONTEXTS) return NULL;
    ShimContext *c = &g_contexts[ctx_id - 1];
    if (!c->alive) return NULL;
    return c;
}

static uint32_t ctx_alloc(JSContext *ctx) {
    for (uint32_t i = 0; i < MAX_CONTEXTS; i++) {
        if (!g_contexts[i].alive) {
            ShimContext *c = &g_contexts[i];
            memset(c, 0, sizeof(*c));
            c->ctx = ctx;
            c->slots = (Slot *)calloc(SLOT_INITIAL_CAP, sizeof(Slot));
            if (!c->slots) return 0;
            c->slot_cap = SLOT_INITIAL_CAP;
            c->slot_count = 1; /* skip slot 0 */
            c->free_head = 0;
            c->scratch = (uint8_t *)malloc(SCRATCH_INITIAL_CAP);
            if (!c->scratch) {
                free(c->slots);
                return 0;
            }
            c->scratch_cap = SCRATCH_INITIAL_CAP;
            c->scratch_len = 0;
            c->alive = true;
            return i + 1;
        }
    }
    return 0;
}

static void ctx_release(ShimContext *c) {
    if (!c->alive) return;
    /* Free any outstanding slot values. */
    for (uint32_t i = 1; i < c->slot_count; i++) {
        if (c->slots[i].refcount > 0) {
            JS_FreeValue(c->ctx, c->slots[i].value);
        }
    }
    free(c->slots);
    free(c->scratch);
    JS_FreeContext(c->ctx);
    memset(c, 0, sizeof(*c));
}

/* Returns a slot ID (>=1) owning one refcount on `value`, or 0 on failure
 * (in which case the caller still owns `value` and must free it). */
static uint32_t slot_alloc(ShimContext *c, JSValue value) {
    uint32_t idx;
    if (c->free_head != 0) {
        idx = c->free_head;
        c->free_head = c->slots[idx].next_free;
    } else {
        if (c->slot_count >= MAX_SLOTS_PER_CONTEXT) return 0;
        if (c->slot_count >= c->slot_cap) {
            uint32_t new_cap = c->slot_cap * 2;
            if (new_cap > MAX_SLOTS_PER_CONTEXT) new_cap = MAX_SLOTS_PER_CONTEXT;
            Slot *n = (Slot *)realloc(c->slots, new_cap * sizeof(Slot));
            if (!n) return 0;
            memset(n + c->slot_cap, 0, (new_cap - c->slot_cap) * sizeof(Slot));
            c->slots = n;
            c->slot_cap = new_cap;
        }
        idx = c->slot_count++;
    }
    c->slots[idx].value = value;
    c->slots[idx].refcount = 1;
    c->slots[idx].next_free = 0;
    return idx;
}

static bool slot_valid(ShimContext *c, uint32_t slot) {
    return slot != 0 && slot < c->slot_count && c->slots[slot].refcount > 0;
}

/* ------------------------------------------------------------------ */
/* Runtime registry                                                    */
/* ------------------------------------------------------------------ */

#define MAX_RUNTIMES 16
static JSRuntime *g_runtimes[MAX_RUNTIMES];

static JSRuntime *rt_lookup(uint32_t rt_id) {
    if (rt_id == 0 || rt_id > MAX_RUNTIMES) return NULL;
    return g_runtimes[rt_id - 1];
}

/* ------------------------------------------------------------------ */
/* Interrupt handler bridge (§6.4)                                     */
/* ------------------------------------------------------------------ */

/* host_interrupt is imported from the Python host. Returning non-zero
 * from this callback aborts the currently executing JS. */
static int shim_interrupt_handler(JSRuntime *rt, void *opaque) {
    (void)rt;
    (void)opaque;
    return host_interrupt();
}

/* ------------------------------------------------------------------ */
/* Runtime lifecycle                                                   */
/* ------------------------------------------------------------------ */

QJS_EXPORT uint32_t qjs_runtime_new(void) {
    for (uint32_t i = 0; i < MAX_RUNTIMES; i++) {
        if (g_runtimes[i] == NULL) {
            JSRuntime *rt = JS_NewRuntime();
            if (!rt) return 0;
            g_runtimes[i] = rt;
            return i + 1;
        }
    }
    return 0;
}

QJS_EXPORT void qjs_runtime_free(uint32_t rt_id) {
    JSRuntime *rt = rt_lookup(rt_id);
    if (!rt) return;
    /* Tear down any contexts bound to this runtime. */
    for (uint32_t i = 0; i < MAX_CONTEXTS; i++) {
        if (g_contexts[i].alive && JS_GetRuntime(g_contexts[i].ctx) == rt) {
            ctx_release(&g_contexts[i]);
        }
    }
    JS_FreeRuntime(rt);
    g_runtimes[rt_id - 1] = NULL;
}

QJS_EXPORT void qjs_runtime_set_memory_limit(uint32_t rt_id, uint64_t bytes) {
    JSRuntime *rt = rt_lookup(rt_id);
    if (!rt) return;
    JS_SetMemoryLimit(rt, (size_t)bytes);
}

QJS_EXPORT void qjs_runtime_set_stack_limit(uint32_t rt_id, uint64_t bytes) {
    JSRuntime *rt = rt_lookup(rt_id);
    if (!rt) return;
    JS_SetMaxStackSize(rt, (size_t)bytes);
}

QJS_EXPORT int32_t qjs_runtime_run_pending_jobs(uint32_t rt_id, uint32_t *out_count) {
    (void)rt_id;
    if (out_count) *out_count = 0;
    return -1; /* Not yet implemented (§17 step 5). */
}

QJS_EXPORT bool qjs_runtime_has_pending_jobs(uint32_t rt_id) {
    JSRuntime *rt = rt_lookup(rt_id);
    if (!rt) return false;
    return JS_IsJobPending(rt);
}

QJS_EXPORT void qjs_runtime_install_interrupt(uint32_t rt_id) {
    JSRuntime *rt = rt_lookup(rt_id);
    if (!rt) return;
    JS_SetInterruptHandler(rt, shim_interrupt_handler, NULL);
}

/* ------------------------------------------------------------------ */
/* Context lifecycle                                                   */
/* ------------------------------------------------------------------ */

QJS_EXPORT uint32_t qjs_context_new(uint32_t rt_id) {
    JSRuntime *rt = rt_lookup(rt_id);
    if (!rt) return 0;
    JSContext *ctx = JS_NewContext(rt);
    if (!ctx) return 0;
    uint32_t id = ctx_alloc(ctx);
    if (id == 0) {
        JS_FreeContext(ctx);
        return 0;
    }
    return id;
}

QJS_EXPORT void qjs_context_free(uint32_t ctx_id) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c) return;
    ctx_release(c);
}

/* ------------------------------------------------------------------ */
/* Slot management                                                     */
/* ------------------------------------------------------------------ */

QJS_EXPORT uint32_t qjs_slot_dup(uint32_t ctx_id, uint32_t slot) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c || !slot_valid(c, slot)) return 0;
    c->slots[slot].refcount++;
    JS_DupValue(c->ctx, c->slots[slot].value);
    return slot;
}

QJS_EXPORT void qjs_slot_drop(uint32_t ctx_id, uint32_t slot) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c || !slot_valid(c, slot)) return;
    if (--c->slots[slot].refcount == 0) {
        JS_FreeValue(c->ctx, c->slots[slot].value);
        c->slots[slot].value = JS_UNDEFINED;
        c->slots[slot].next_free = c->free_head;
        c->free_head = slot;
    } else {
        JS_FreeValue(c->ctx, c->slots[slot].value);
    }
}

/* ------------------------------------------------------------------ */
/* Eval                                                                */
/* ------------------------------------------------------------------ */

QJS_EXPORT int32_t qjs_eval(uint32_t ctx_id,
                            uint32_t code_ptr, uint32_t code_len,
                            uint32_t flags,
                            uint32_t *out_slot) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c || !out_slot) return -1;
    *out_slot = 0;

    int eval_flags = JS_EVAL_TYPE_GLOBAL;
    if (flags & 0x1) eval_flags = JS_EVAL_TYPE_MODULE;
    if (flags & 0x2) eval_flags |= JS_EVAL_FLAG_COMPILE_ONLY;
    if (flags & 0x4) eval_flags |= JS_EVAL_FLAG_STRICT;

    /* §6.4: quickjs-ng's tokenizer one-past-overreads the input buffer
     * during lookahead despite being given an explicit length; copy into
     * a NUL-terminated buffer so callers don't have to pad. */
    char *code = (char *)malloc((size_t)code_len + 1);
    if (!code) return -1;
    if (code_len > 0) memcpy(code, (const void *)(uintptr_t)code_ptr, code_len);
    code[code_len] = '\0';

    JSValue result = JS_Eval(c->ctx, code, (size_t)code_len, "<eval>", eval_flags);
    free(code);

    if (JS_IsException(result)) {
        JSValue exc = JS_GetException(c->ctx);
        uint32_t slot = slot_alloc(c, exc);
        if (slot == 0) {
            JS_FreeValue(c->ctx, exc);
            return -1;
        }
        *out_slot = slot;
        return 1;
    }

    uint32_t slot = slot_alloc(c, result);
    if (slot == 0) {
        JS_FreeValue(c->ctx, result);
        return -1;
    }
    *out_slot = slot;
    return 0;
}

/* ------------------------------------------------------------------ */
/* Marshaling — minimal: numbers only (§8)                             */
/* ------------------------------------------------------------------ */

static bool scratch_reserve(ShimContext *c, uint32_t need) {
    if (need <= c->scratch_cap) return true;
    uint32_t new_cap = c->scratch_cap;
    while (new_cap < need) new_cap *= 2;
    uint8_t *n = (uint8_t *)realloc(c->scratch, new_cap);
    if (!n) return false;
    c->scratch = n;
    c->scratch_cap = new_cap;
    return true;
}

static void scratch_reset(ShimContext *c) {
    c->scratch_len = 0;
}

static void be_store_u64(uint8_t *p, uint64_t v) {
    p[0] = (uint8_t)(v >> 56);
    p[1] = (uint8_t)(v >> 48);
    p[2] = (uint8_t)(v >> 40);
    p[3] = (uint8_t)(v >> 32);
    p[4] = (uint8_t)(v >> 24);
    p[5] = (uint8_t)(v >> 16);
    p[6] = (uint8_t)(v >> 8);
    p[7] = (uint8_t)v;
}

/* Encode a JS number as MessagePack float64. §8: all JS numbers are f64
 * on the wire, even integer-valued ones, to preserve JS semantics. */
static int32_t encode_number(ShimContext *c, double d) {
    if (!scratch_reserve(c, 9)) return -1;
    c->scratch[0] = 0xcb; /* float64 */
    union { double d; uint64_t u; } conv;
    conv.d = d;
    be_store_u64(&c->scratch[1], conv.u);
    c->scratch_len = 9;
    return 0;
}

QJS_EXPORT int32_t qjs_to_msgpack(uint32_t ctx_id, uint32_t slot,
                                  uint32_t *out_ptr, uint32_t *out_len) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c || !out_ptr || !out_len || !slot_valid(c, slot)) return -1;
    scratch_reset(c);

    JSValue v = c->slots[slot].value;
    int tag = JS_VALUE_GET_TAG(v);

    int32_t rc;
    if (tag == JS_TAG_INT) {
        rc = encode_number(c, (double)JS_VALUE_GET_INT(v));
    } else if (JS_TAG_IS_FLOAT64(tag)) {
        rc = encode_number(c, JS_VALUE_GET_FLOAT64(v));
    } else {
        /* All other branches land in later commits. */
        return -1;
    }

    if (rc != 0) return rc;
    *out_ptr = (uint32_t)(uintptr_t)c->scratch;
    *out_len = c->scratch_len;
    return 0;
}

QJS_EXPORT int32_t qjs_from_msgpack(uint32_t ctx_id,
                                    uint32_t data_ptr, uint32_t data_len,
                                    uint32_t *out_slot) {
    (void)ctx_id; (void)data_ptr; (void)data_len;
    if (out_slot) *out_slot = 0;
    return -1; /* Not yet implemented. */
}

QJS_EXPORT int32_t qjs_exception_to_msgpack(uint32_t ctx_id, uint32_t exc_slot,
                                            uint32_t *out_ptr, uint32_t *out_len) {
    (void)ctx_id; (void)exc_slot;
    if (out_ptr) *out_ptr = 0;
    if (out_len) *out_len = 0;
    return -1; /* Not yet implemented. */
}

/* ------------------------------------------------------------------ */
/* Stubs for §6.2 exports that aren't needed yet                       */
/* ------------------------------------------------------------------ */

QJS_EXPORT int32_t qjs_get_global_object(uint32_t ctx_id, uint32_t *out_slot) {
    (void)ctx_id;
    if (out_slot) *out_slot = 0;
    return -1;
}

QJS_EXPORT int32_t qjs_get_prop(uint32_t ctx_id, uint32_t obj_slot,
                                uint32_t key_ptr, uint32_t key_len,
                                uint32_t *out_slot) {
    (void)ctx_id; (void)obj_slot; (void)key_ptr; (void)key_len;
    if (out_slot) *out_slot = 0;
    return -1;
}

QJS_EXPORT int32_t qjs_set_prop(uint32_t ctx_id, uint32_t obj_slot,
                                uint32_t key_ptr, uint32_t key_len,
                                uint32_t val_slot) {
    (void)ctx_id; (void)obj_slot; (void)key_ptr; (void)key_len; (void)val_slot;
    return -1;
}

QJS_EXPORT int32_t qjs_get_prop_u32(uint32_t ctx_id, uint32_t obj_slot,
                                    uint32_t index, uint32_t *out_slot) {
    (void)ctx_id; (void)obj_slot; (void)index;
    if (out_slot) *out_slot = 0;
    return -1;
}

QJS_EXPORT int32_t qjs_call(uint32_t ctx_id, uint32_t fn_slot, uint32_t this_slot,
                            uint32_t argc, uint32_t argv_ptr,
                            uint32_t *out_slot) {
    (void)ctx_id; (void)fn_slot; (void)this_slot; (void)argc; (void)argv_ptr;
    if (out_slot) *out_slot = 0;
    return -1;
}

QJS_EXPORT int32_t qjs_new_instance(uint32_t ctx_id, uint32_t ctor_slot,
                                    uint32_t argc, uint32_t argv_ptr,
                                    uint32_t *out_slot) {
    (void)ctx_id; (void)ctor_slot; (void)argc; (void)argv_ptr;
    if (out_slot) *out_slot = 0;
    return -1;
}

QJS_EXPORT uint32_t qjs_type_of(uint32_t ctx_id, uint32_t slot) {
    (void)ctx_id; (void)slot;
    return 0; /* "null" sentinel; real mapping lands with handles. */
}

QJS_EXPORT bool qjs_is_promise(uint32_t ctx_id, uint32_t slot) {
    (void)ctx_id; (void)slot;
    return false;
}

QJS_EXPORT int32_t qjs_promise_state(uint32_t ctx_id, uint32_t slot) {
    (void)ctx_id; (void)slot;
    return -1;
}

QJS_EXPORT int32_t qjs_register_host_function(uint32_t ctx_id,
                                              uint32_t name_ptr, uint32_t name_len,
                                              uint32_t fn_id) {
    (void)ctx_id; (void)name_ptr; (void)name_len; (void)fn_id;
    return -1;
}

/* ------------------------------------------------------------------ */
/* Guest memory — pass through to libc malloc/free                     */
/* ------------------------------------------------------------------ */

QJS_EXPORT uint32_t qjs_malloc(uint32_t size) {
    void *p = malloc(size);
    return (uint32_t)(uintptr_t)p;
}

QJS_EXPORT void qjs_free(uint32_t ptr) {
    free((void *)(uintptr_t)ptr);
}
