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

typedef struct DynBuf {
    uint8_t *data;
    uint32_t cap;
    uint32_t len;
} DynBuf;

typedef struct ShimContext {
    JSContext *ctx;
    Slot *slots;
    uint32_t slot_cap;
    uint32_t slot_count; /* high-water mark of allocated slots */
    uint32_t free_head; /* 0 means empty */
    DynBuf scratch;
    bool alive;
} ShimContext;

static bool dynbuf_init(DynBuf *b, uint32_t cap) {
    b->data = (uint8_t *)malloc(cap);
    if (!b->data) return false;
    b->cap = cap;
    b->len = 0;
    return true;
}

static void dynbuf_free(DynBuf *b) {
    free(b->data);
    b->data = NULL;
    b->cap = b->len = 0;
}

static bool dynbuf_reserve(DynBuf *b, uint32_t need) {
    if (need <= b->cap) return true;
    uint32_t new_cap = b->cap > 0 ? b->cap : 64;
    while (new_cap < need) new_cap *= 2;
    uint8_t *n = (uint8_t *)realloc(b->data, new_cap);
    if (!n) return false;
    b->data = n;
    b->cap = new_cap;
    return true;
}

static void dynbuf_reset(DynBuf *b) { b->len = 0; }

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
            if (!dynbuf_init(&c->scratch, SCRATCH_INITIAL_CAP)) {
                free(c->slots);
                return 0;
            }
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
    dynbuf_free(&c->scratch);
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

/* ------------------------------------------------------------------ */
/* Encoders                                                            */
/*                                                                     */
/* Each encoder writes at `off` inside the shim-owned scratch buffer   */
/* and returns the new trailing offset (number of bytes written so     */
/* far), or -1 on failure. scratch_reserve may realloc, so encoders    */
/* only resolve `c->scratch` to a pointer *after* the reserve call.    */
/* Recursive values (arrays, objects) use the same return-new-offset   */
/* convention so each child append runs in sequence.                   */
/* ------------------------------------------------------------------ */

static int32_t encode_value(ShimContext *c, DynBuf *b, JSValue v, int32_t off, int depth);

/* Max nested container depth. QuickJS's own parser caps at ~1000; we
 * stay well below that to keep the recursion predictable. */
#define MARSHAL_MAX_DEPTH 128

static int32_t buf_write_u8(DynBuf *b, int32_t off, uint8_t byte) {
    if (!dynbuf_reserve(b, (uint32_t)off + 1)) return -1;
    b->data[off] = byte;
    return off + 1;
}

static int32_t encode_number(DynBuf *b, int32_t off, double d) {
    if (!dynbuf_reserve(b, (uint32_t)off + 9)) return -1;
    uint8_t *p = b->data + off;
    p[0] = 0xcb; /* float64 */
    union { double d; uint64_t u; } conv;
    conv.d = d;
    be_store_u64(p + 1, conv.u);
    return off + 9;
}

static int32_t encode_str_bytes(DynBuf *b, int32_t off,
                                const uint8_t *bytes, size_t len) {
    uint32_t header_len;
    if (len <= 31) header_len = 1;
    else if (len <= 0xff) header_len = 2;
    else if (len <= 0xffff) header_len = 3;
    else if (len <= 0xffffffffu) header_len = 5;
    else return -1;
    if (!dynbuf_reserve(b, (uint32_t)off + header_len + (uint32_t)len)) return -1;
    uint8_t *p = b->data + off;
    if (len <= 31) {
        p[0] = (uint8_t)(0xa0 | len);
    } else if (len <= 0xff) {
        p[0] = 0xd9; p[1] = (uint8_t)len;
    } else if (len <= 0xffff) {
        p[0] = 0xda;
        p[1] = (uint8_t)(len >> 8);
        p[2] = (uint8_t)len;
    } else {
        p[0] = 0xdb;
        p[1] = (uint8_t)(len >> 24);
        p[2] = (uint8_t)(len >> 16);
        p[3] = (uint8_t)(len >> 8);
        p[4] = (uint8_t)len;
    }
    if (len > 0) memcpy(p + header_len, bytes, len);
    return off + (int32_t)header_len + (int32_t)len;
}

static int32_t encode_bin_bytes(DynBuf *b, int32_t off,
                                const uint8_t *bytes, size_t len) {
    uint32_t header_len;
    if (len <= 0xff) header_len = 2;
    else if (len <= 0xffff) header_len = 3;
    else if (len <= 0xffffffffu) header_len = 5;
    else return -1;
    if (!dynbuf_reserve(b, (uint32_t)off + header_len + (uint32_t)len)) return -1;
    uint8_t *p = b->data + off;
    if (len <= 0xff) {
        p[0] = 0xc4; p[1] = (uint8_t)len;
    } else if (len <= 0xffff) {
        p[0] = 0xc5;
        p[1] = (uint8_t)(len >> 8);
        p[2] = (uint8_t)len;
    } else {
        p[0] = 0xc6;
        p[1] = (uint8_t)(len >> 24);
        p[2] = (uint8_t)(len >> 16);
        p[3] = (uint8_t)(len >> 8);
        p[4] = (uint8_t)len;
    }
    if (len > 0) memcpy(p + header_len, bytes, len);
    return off + (int32_t)header_len + (int32_t)len;
}

/* Encode a BigInt as msgpack ext1 (body = UTF-8 decimal string). §8.
 * JS_ToCStringLen on a BigInt yields the decimal form with no "n" suffix
 * and a leading "-" for negatives. */
static int32_t encode_bigint(ShimContext *c, DynBuf *b, int32_t off, JSValue v) {
    size_t slen;
    const char *s = JS_ToCStringLen(c->ctx, &slen, v);
    if (!s) return -1;

    uint32_t header_len;
    switch (slen) {
        case 1: case 2: case 4: case 8: case 16:
            header_len = 2; break;
        default:
            if (slen <= 0xff) header_len = 3;
            else if (slen <= 0xffff) header_len = 4;
            else if (slen <= 0xffffffffu) header_len = 6;
            else { JS_FreeCString(c->ctx, s); return -1; }
            break;
    }

    if (!dynbuf_reserve(b, (uint32_t)off + header_len + (uint32_t)slen)) {
        JS_FreeCString(c->ctx, s);
        return -1;
    }
    uint8_t *p = b->data + off;
    switch (slen) {
        case 1:  p[0] = 0xd4; p[1] = 0x01; break;
        case 2:  p[0] = 0xd5; p[1] = 0x01; break;
        case 4:  p[0] = 0xd6; p[1] = 0x01; break;
        case 8:  p[0] = 0xd7; p[1] = 0x01; break;
        case 16: p[0] = 0xd8; p[1] = 0x01; break;
        default:
            if (slen <= 0xff) {
                p[0] = 0xc7; p[1] = (uint8_t)slen; p[2] = 0x01;
            } else if (slen <= 0xffff) {
                p[0] = 0xc8;
                p[1] = (uint8_t)(slen >> 8);
                p[2] = (uint8_t)slen;
                p[3] = 0x01;
            } else {
                p[0] = 0xc9;
                p[1] = (uint8_t)(slen >> 24);
                p[2] = (uint8_t)(slen >> 16);
                p[3] = (uint8_t)(slen >> 8);
                p[4] = (uint8_t)slen;
                p[5] = 0x01;
            }
            break;
    }
    memcpy(p + header_len, s, slen);
    JS_FreeCString(c->ctx, s);
    return off + (int32_t)header_len + (int32_t)slen;
}

static int32_t encode_array_header(DynBuf *b, int32_t off, uint32_t n) {
    if (n <= 15) {
        return buf_write_u8(b, off, (uint8_t)(0x90 | n));
    } else if (n <= 0xffff) {
        if (!dynbuf_reserve(b, (uint32_t)off + 3)) return -1;
        uint8_t *p = b->data + off;
        p[0] = 0xdc;
        p[1] = (uint8_t)(n >> 8);
        p[2] = (uint8_t)n;
        return off + 3;
    } else {
        if (!dynbuf_reserve(b, (uint32_t)off + 5)) return -1;
        uint8_t *p = b->data + off;
        p[0] = 0xdd;
        p[1] = (uint8_t)(n >> 24);
        p[2] = (uint8_t)(n >> 16);
        p[3] = (uint8_t)(n >> 8);
        p[4] = (uint8_t)n;
        return off + 5;
    }
}

static int32_t encode_array(ShimContext *c, DynBuf *b, int32_t off, JSValue arr, int depth) {
    int64_t length64 = 0;
    if (JS_GetLength(c->ctx, arr, &length64) < 0) return -1;
    if (length64 < 0 || length64 > 0xffffffffLL) return -1;
    uint32_t length = (uint32_t)length64;

    off = encode_array_header(b, off, length);
    if (off < 0) return -1;

    for (uint32_t i = 0; i < length; i++) {
        JSValue elem = JS_GetPropertyUint32(c->ctx, arr, i);
        if (JS_IsException(elem)) return -1;
        off = encode_value(c, b, elem, off, depth + 1);
        JS_FreeValue(c->ctx, elem);
        if (off < 0) return -1;
    }
    return off;
}

static int32_t encode_map_header(DynBuf *b, int32_t off, uint32_t n) {
    if (n <= 15) {
        return buf_write_u8(b, off, (uint8_t)(0x80 | n));
    } else if (n <= 0xffff) {
        if (!dynbuf_reserve(b, (uint32_t)off + 3)) return -1;
        uint8_t *p = b->data + off;
        p[0] = 0xde;
        p[1] = (uint8_t)(n >> 8);
        p[2] = (uint8_t)n;
        return off + 3;
    } else {
        if (!dynbuf_reserve(b, (uint32_t)off + 5)) return -1;
        uint8_t *p = b->data + off;
        p[0] = 0xdf;
        p[1] = (uint8_t)(n >> 24);
        p[2] = (uint8_t)(n >> 16);
        p[3] = (uint8_t)(n >> 8);
        p[4] = (uint8_t)n;
        return off + 5;
    }
}

/* §8: plain Object → msgpack map with str keys, insertion-ordered.
 * JS_GetOwnPropertyNames with JS_GPN_STRING_MASK|JS_GPN_ENUM_ONLY returns
 * own enumerable string-keyed properties in insertion order, matching
 * for...in / Object.keys / JSON.stringify semantics. */
static int32_t encode_object(ShimContext *c, DynBuf *b, int32_t off, JSValue obj, int depth) {
    JSPropertyEnum *props = NULL;
    uint32_t n = 0;
    if (JS_GetOwnPropertyNames(c->ctx, &props, &n, obj,
                               JS_GPN_STRING_MASK | JS_GPN_ENUM_ONLY) < 0) {
        return -1;
    }

    int32_t new_off = encode_map_header(b, off, n);
    if (new_off < 0) goto fail;

    for (uint32_t i = 0; i < n; i++) {
        size_t klen;
        const char *key = JS_AtomToCStringLen(c->ctx, &klen, props[i].atom);
        if (!key) { new_off = -1; goto fail; }

        new_off = encode_str_bytes(b, new_off, (const uint8_t *)key, klen);
        JS_FreeCString(c->ctx, key);
        if (new_off < 0) goto fail;

        JSValue val = JS_GetProperty(c->ctx, obj, props[i].atom);
        if (JS_IsException(val)) { new_off = -1; goto fail; }
        new_off = encode_value(c, b, val, new_off, depth + 1);
        JS_FreeValue(c->ctx, val);
        if (new_off < 0) goto fail;
    }

fail:
    JS_FreePropertyEnum(c->ctx, props, n);
    return new_off;
}

static int32_t encode_value(ShimContext *c, DynBuf *b, JSValue v, int32_t off, int depth) {
    if (depth > MARSHAL_MAX_DEPTH) return -1;
    int tag = JS_VALUE_GET_TAG(v);

    if (tag == JS_TAG_INT) {
        return encode_number(b, off, (double)JS_VALUE_GET_INT(v));
    }
    if (JS_TAG_IS_FLOAT64(tag)) {
        return encode_number(b, off, JS_VALUE_GET_FLOAT64(v));
    }
    if (tag == JS_TAG_BOOL) {
        return buf_write_u8(b, off, JS_VALUE_GET_BOOL(v) ? 0xc3 : 0xc2);
    }
    if (tag == JS_TAG_NULL) {
        return buf_write_u8(b, off, 0xc0);
    }
    if (tag == JS_TAG_UNDEFINED) {
        /* §8: ext type 0, empty body. ext8 is the smallest zero-length ext. */
        off = buf_write_u8(b, off, 0xc7);
        if (off < 0) return -1;
        off = buf_write_u8(b, off, 0x00);
        if (off < 0) return -1;
        return buf_write_u8(b, off, 0x00);
    }
    if (tag == JS_TAG_STRING || tag == JS_TAG_STRING_ROPE) {
        size_t slen;
        const char *s = JS_ToCStringLen(c->ctx, &slen, v);
        if (!s) return -1;
        int32_t rc = encode_str_bytes(b, off, (const uint8_t *)s, slen);
        JS_FreeCString(c->ctx, s);
        return rc;
    }
    if (tag == JS_TAG_BIG_INT || tag == JS_TAG_SHORT_BIG_INT) {
        return encode_bigint(c, b, off, v);
    }
    if (JS_GetTypedArrayType(v) == JS_TYPED_ARRAY_UINT8) {
        size_t byte_offset = 0, byte_length = 0;
        JSValue ab = JS_GetTypedArrayBuffer(c->ctx, v, &byte_offset, &byte_length, NULL);
        if (JS_IsException(ab)) return -1;
        size_t ab_len = 0;
        uint8_t *ab_data = JS_GetArrayBuffer(c->ctx, &ab_len, ab);
        if (!ab_data) { JS_FreeValue(c->ctx, ab); return -1; }
        int32_t rc = encode_bin_bytes(b, off, ab_data + byte_offset, byte_length);
        JS_FreeValue(c->ctx, ab);
        return rc;
    }
    if (JS_IsArray(v)) {
        return encode_array(c, b, off, v, depth);
    }
    if (JS_IsObject(v)) {
        /* §8: functions in eval results are not marshalable — they must be
         * held as handles instead. Same for Promises (drive them first) and
         * typed arrays other than Uint8Array (already handled above). */
        /* TODO(handles): §8 recommends surfacing "use eval_handle" in the
         * resulting MarshalError message. Wording lands when eval_handle is
         * a real API to point at. */
        if (JS_IsFunction(c->ctx, v)) return -1;
        if (JS_IsPromise(v)) return -1;
        if (JS_GetTypedArrayType(v) >= 0) return -1;
        return encode_object(c, b, off, v, depth);
    }
    if (tag == JS_TAG_SYMBOL) {
        /* §8: symbols are not marshalable in eval results. */
        /* TODO(handles): same as above — error copy polish lands with
         * eval_handle. */
        return -1;
    }
    /* Unknown tag. */
    return -1;
}

QJS_EXPORT int32_t qjs_to_msgpack(uint32_t ctx_id, uint32_t slot,
                                  uint32_t *out_ptr, uint32_t *out_len) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c || !out_ptr || !out_len || !slot_valid(c, slot)) return -1;
    dynbuf_reset(&c->scratch);

    int32_t end = encode_value(c, &c->scratch, c->slots[slot].value, 0, 0);
    if (end < 0) return -1;
    c->scratch.len = (uint32_t)end;
    *out_ptr = (uint32_t)(uintptr_t)c->scratch.data;
    *out_len = c->scratch.len;
    return 0;
}

/* ------------------------------------------------------------------ */
/* MessagePack decode (host → JS)                                      */
/* ------------------------------------------------------------------ */

static uint64_t be_load_u64(const uint8_t *p) {
    return ((uint64_t)p[0] << 56) | ((uint64_t)p[1] << 48) |
           ((uint64_t)p[2] << 40) | ((uint64_t)p[3] << 32) |
           ((uint64_t)p[4] << 24) | ((uint64_t)p[5] << 16) |
           ((uint64_t)p[6] << 8)  |  (uint64_t)p[7];
}

typedef struct DecCursor {
    const uint8_t *data;
    uint32_t len;
    uint32_t off;
    bool error;
} DecCursor;

static bool dec_need(DecCursor *c, uint32_t n) {
    if (c->error) return false;
    if (c->off + n > c->len) { c->error = true; return false; }
    return true;
}

static uint8_t dec_u8(DecCursor *c) {
    if (!dec_need(c, 1)) return 0;
    return c->data[c->off++];
}

static uint32_t dec_u16(DecCursor *c) {
    if (!dec_need(c, 2)) return 0;
    uint32_t v = ((uint32_t)c->data[c->off] << 8) | c->data[c->off + 1];
    c->off += 2;
    return v;
}

static uint32_t dec_u32(DecCursor *c) {
    if (!dec_need(c, 4)) return 0;
    uint32_t v = ((uint32_t)c->data[c->off] << 24) |
                 ((uint32_t)c->data[c->off + 1] << 16) |
                 ((uint32_t)c->data[c->off + 2] << 8) |
                 c->data[c->off + 3];
    c->off += 4;
    return v;
}

static const uint8_t *dec_take(DecCursor *c, uint32_t n) {
    if (!dec_need(c, n)) return NULL;
    const uint8_t *p = c->data + c->off;
    c->off += n;
    return p;
}

/* Decode a BigInt by calling the BigInt global as a constructor with the
 * decimal string as its argument. See user note in the commit thread:
 * this is simpler than walking the decimal ourselves and matches JS
 * semantics precisely (sign, leading zeros, overflow). */
static JSValue decode_bigint(JSContext *ctx, const char *decimal, size_t len) {
    JSValue global = JS_GetGlobalObject(ctx);
    JSValue bigint_ctor = JS_GetPropertyStr(ctx, global, "BigInt");
    JS_FreeValue(ctx, global);
    if (JS_IsException(bigint_ctor)) return bigint_ctor;
    JSValue arg = JS_NewStringLen(ctx, decimal, len);
    if (JS_IsException(arg)) {
        JS_FreeValue(ctx, bigint_ctor);
        return arg;
    }
    JSValue result = JS_Call(ctx, bigint_ctor, JS_UNDEFINED, 1, &arg);
    JS_FreeValue(ctx, arg);
    JS_FreeValue(ctx, bigint_ctor);
    return result;
}

static JSValue decode_value(ShimContext *c, DecCursor *cur, int depth);

#define DEC_MAX_DEPTH MARSHAL_MAX_DEPTH

static JSValue decode_ext(ShimContext *c, DecCursor *cur, uint32_t len) {
    uint8_t ext_type = dec_u8(cur);
    const uint8_t *body = dec_take(cur, len);
    if (cur->error) return JS_EXCEPTION;
    if (ext_type == 0) { /* undefined */
        if (len != 0) { cur->error = true; return JS_EXCEPTION; }
        return JS_UNDEFINED;
    }
    if (ext_type == 1) { /* bigint: UTF-8 decimal */
        return decode_bigint(c->ctx, (const char *)body, len);
    }
    cur->error = true;
    return JS_EXCEPTION;
}

static JSValue decode_array(ShimContext *c, DecCursor *cur, uint32_t count, int depth) {
    JSValue arr = JS_NewArray(c->ctx);
    if (JS_IsException(arr)) return arr;
    for (uint32_t i = 0; i < count; i++) {
        JSValue elem = decode_value(c, cur, depth + 1);
        if (JS_IsException(elem)) {
            JS_FreeValue(c->ctx, arr);
            return elem;
        }
        if (JS_SetPropertyUint32(c->ctx, arr, i, elem) < 0) {
            /* JS_SetPropertyUint32 consumes `elem` on both success and
             * failure paths. */
            JS_FreeValue(c->ctx, arr);
            return JS_EXCEPTION;
        }
    }
    return arr;
}

static JSValue decode_str_value(JSContext *ctx, DecCursor *cur, uint32_t len) {
    const uint8_t *body = dec_take(cur, len);
    if (cur->error) return JS_EXCEPTION;
    return JS_NewStringLen(ctx, (const char *)body, len);
}

static JSValue decode_map(ShimContext *c, DecCursor *cur, uint32_t count, int depth) {
    JSValue obj = JS_NewObject(c->ctx);
    if (JS_IsException(obj)) return obj;
    for (uint32_t i = 0; i < count; i++) {
        /* Key: must be a msgpack str per §8. Read it inline so we can use
         * JS_SetPropertyStr which takes a NUL-terminated C string. */
        uint8_t kb = dec_u8(cur);
        uint32_t klen;
        if (kb >= 0xa0 && kb <= 0xbf) {
            klen = kb & 0x1f;
        } else if (kb == 0xd9) {
            klen = dec_u8(cur);
        } else if (kb == 0xda) {
            klen = dec_u16(cur);
        } else if (kb == 0xdb) {
            klen = dec_u32(cur);
        } else {
            cur->error = true;
            JS_FreeValue(c->ctx, obj);
            return JS_EXCEPTION;
        }
        const uint8_t *kbody = dec_take(cur, klen);
        if (cur->error) {
            JS_FreeValue(c->ctx, obj);
            return JS_EXCEPTION;
        }
        /* JS_SetPropertyStr needs a NUL-terminated key. Copy into a small
         * scratch buffer. Keys shouldn't be huge in practice (agent
         * payloads), so stack-alloc up to 256 bytes and heap beyond. */
        char stack_buf[256];
        char *kcopy = klen < sizeof(stack_buf) ? stack_buf :
                                                 (char *)malloc((size_t)klen + 1);
        if (!kcopy) {
            JS_FreeValue(c->ctx, obj);
            return JS_EXCEPTION;
        }
        memcpy(kcopy, kbody, klen);
        kcopy[klen] = '\0';

        JSValue val = decode_value(c, cur, depth + 1);
        if (JS_IsException(val)) {
            if (kcopy != stack_buf) free(kcopy);
            JS_FreeValue(c->ctx, obj);
            return val;
        }
        int rc = JS_SetPropertyStr(c->ctx, obj, kcopy, val);
        if (kcopy != stack_buf) free(kcopy);
        if (rc < 0) {
            JS_FreeValue(c->ctx, obj);
            return JS_EXCEPTION;
        }
    }
    return obj;
}

static JSValue decode_value(ShimContext *c, DecCursor *cur, int depth) {
    if (depth > DEC_MAX_DEPTH) { cur->error = true; return JS_EXCEPTION; }
    uint8_t b = dec_u8(cur);
    if (cur->error) return JS_EXCEPTION;

    /* positive fixint */
    if (b < 0x80) return JS_NewInt32(c->ctx, (int32_t)b);
    /* fixmap */
    if (b >= 0x80 && b <= 0x8f) {
        return decode_map(c, cur, b & 0x0f, depth);
    }
    /* fixarray */
    if (b >= 0x90 && b <= 0x9f) {
        return decode_array(c, cur, b & 0x0f, depth);
    }
    /* fixstr */
    if (b >= 0xa0 && b <= 0xbf) {
        return decode_str_value(c->ctx, cur, b & 0x1f);
    }

    switch (b) {
        case 0xc0: return JS_NULL;
        case 0xc2: return JS_FALSE;
        case 0xc3: return JS_TRUE;

        case 0xc4: { /* bin 8 */
            uint32_t n = dec_u8(cur);
            const uint8_t *body = dec_take(cur, n);
            if (cur->error) return JS_EXCEPTION;
            return JS_NewUint8ArrayCopy(c->ctx, body, n);
        }
        case 0xc5: {
            uint32_t n = dec_u16(cur);
            const uint8_t *body = dec_take(cur, n);
            if (cur->error) return JS_EXCEPTION;
            return JS_NewUint8ArrayCopy(c->ctx, body, n);
        }
        case 0xc6: {
            uint32_t n = dec_u32(cur);
            const uint8_t *body = dec_take(cur, n);
            if (cur->error) return JS_EXCEPTION;
            return JS_NewUint8ArrayCopy(c->ctx, body, n);
        }

        case 0xc7: { /* ext 8 */
            uint32_t n = dec_u8(cur);
            return decode_ext(c, cur, n);
        }
        case 0xc8: {
            uint32_t n = dec_u16(cur);
            return decode_ext(c, cur, n);
        }
        case 0xc9: {
            uint32_t n = dec_u32(cur);
            return decode_ext(c, cur, n);
        }

        case 0xca: { /* float32 — not emitted by shim but msgpack-legal */
            const uint8_t *p = dec_take(cur, 4);
            if (cur->error) return JS_EXCEPTION;
            uint32_t bits = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
                            ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
            union { uint32_t u; float f; } conv;
            conv.u = bits;
            return JS_NewFloat64(c->ctx, (double)conv.f);
        }
        case 0xcb: { /* float64 */
            const uint8_t *p = dec_take(cur, 8);
            if (cur->error) return JS_EXCEPTION;
            union { uint64_t u; double d; } conv;
            conv.u = be_load_u64(p);
            return JS_NewFloat64(c->ctx, conv.d);
        }

        /* uint 8/16/32/64 — Python int outside safe range goes through
         * ext1 bigint, but inside range uses float64. We still accept
         * these for robustness (other producers may emit them). */
        case 0xcc: return JS_NewInt32(c->ctx, (int32_t)dec_u8(cur));
        case 0xcd: return JS_NewInt32(c->ctx, (int32_t)dec_u16(cur));
        case 0xce: {
            uint32_t n = dec_u32(cur);
            return JS_NewInt64(c->ctx, (int64_t)n);
        }
        case 0xcf: {
            const uint8_t *p = dec_take(cur, 8);
            if (cur->error) return JS_EXCEPTION;
            uint64_t n = be_load_u64(p);
            /* Numbers outside safe-integer range should have come through
             * as bigint; if someone emits a large uint64, best-effort
             * convert via f64 (lossy but defined). */
            return JS_NewFloat64(c->ctx, (double)n);
        }
        /* int 8/16/32/64 */
        case 0xd0: return JS_NewInt32(c->ctx, (int8_t)dec_u8(cur));
        case 0xd1: return JS_NewInt32(c->ctx, (int16_t)dec_u16(cur));
        case 0xd2: return JS_NewInt32(c->ctx, (int32_t)dec_u32(cur));
        case 0xd3: {
            const uint8_t *p = dec_take(cur, 8);
            if (cur->error) return JS_EXCEPTION;
            uint64_t u = be_load_u64(p);
            int64_t n = (int64_t)u;
            return JS_NewInt64(c->ctx, n);
        }
        /* negative fixint */

        /* fixext 1/2/4/8/16 */
        case 0xd4: return decode_ext(c, cur, 1);
        case 0xd5: return decode_ext(c, cur, 2);
        case 0xd6: return decode_ext(c, cur, 4);
        case 0xd7: return decode_ext(c, cur, 8);
        case 0xd8: return decode_ext(c, cur, 16);

        /* str 8/16/32 */
        case 0xd9: return decode_str_value(c->ctx, cur, dec_u8(cur));
        case 0xda: return decode_str_value(c->ctx, cur, dec_u16(cur));
        case 0xdb: return decode_str_value(c->ctx, cur, dec_u32(cur));

        /* array 16/32 */
        case 0xdc: return decode_array(c, cur, dec_u16(cur), depth);
        case 0xdd: return decode_array(c, cur, dec_u32(cur), depth);

        /* map 16/32 */
        case 0xde: return decode_map(c, cur, dec_u16(cur), depth);
        case 0xdf: return decode_map(c, cur, dec_u32(cur), depth);
    }

    /* negative fixint */
    if (b >= 0xe0) return JS_NewInt32(c->ctx, (int32_t)(int8_t)b);

    cur->error = true;
    return JS_EXCEPTION;
}

QJS_EXPORT int32_t qjs_from_msgpack(uint32_t ctx_id,
                                    uint32_t data_ptr, uint32_t data_len,
                                    uint32_t *out_slot) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c || !out_slot) return -1;
    *out_slot = 0;

    DecCursor cur = {
        .data = (const uint8_t *)(uintptr_t)data_ptr,
        .len = data_len,
        .off = 0,
        .error = false,
    };
    JSValue v = decode_value(c, &cur, 0);
    if (cur.error || JS_IsException(v)) {
        if (!JS_IsException(v)) JS_FreeValue(c->ctx, v);
        else JS_FreeValue(c->ctx, JS_GetException(c->ctx));
        return -1;
    }
    if (cur.off != cur.len) {
        JS_FreeValue(c->ctx, v);
        return -1; /* trailing bytes */
    }
    uint32_t slot = slot_alloc(c, v);
    if (slot == 0) {
        JS_FreeValue(c->ctx, v);
        return -1;
    }
    *out_slot = slot;
    return 0;
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
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c || !out_slot) return -1;
    *out_slot = 0;
    JSValue global = JS_GetGlobalObject(c->ctx);
    if (JS_IsException(global)) return -1;
    uint32_t slot = slot_alloc(c, global);
    if (slot == 0) {
        JS_FreeValue(c->ctx, global);
        return -1;
    }
    *out_slot = slot;
    return 0;
}

/* Helper: copy (key_ptr, key_len) into a NUL-terminated stack/heap buffer.
 * Returns NULL on OOM; caller must free if *on_heap is true. */
static char *key_to_cstr(uint32_t key_ptr, uint32_t key_len,
                        char *stack_buf, size_t stack_cap, bool *on_heap) {
    const char *src = (const char *)(uintptr_t)key_ptr;
    char *dst;
    if (key_len < stack_cap) {
        dst = stack_buf;
        *on_heap = false;
    } else {
        dst = (char *)malloc((size_t)key_len + 1);
        if (!dst) return NULL;
        *on_heap = true;
    }
    if (key_len > 0) memcpy(dst, src, key_len);
    dst[key_len] = '\0';
    return dst;
}

QJS_EXPORT int32_t qjs_get_prop(uint32_t ctx_id, uint32_t obj_slot,
                                uint32_t key_ptr, uint32_t key_len,
                                uint32_t *out_slot) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c || !out_slot || !slot_valid(c, obj_slot)) return -1;
    *out_slot = 0;

    char stack_key[256];
    bool on_heap = false;
    char *key = key_to_cstr(key_ptr, key_len, stack_key, sizeof(stack_key), &on_heap);
    if (!key) return -1;

    JSValue obj = c->slots[obj_slot].value;
    JSValue v = JS_GetPropertyStr(c->ctx, obj, key);
    if (on_heap) free(key);

    if (JS_IsException(v)) {
        JSValue exc = JS_GetException(c->ctx);
        uint32_t slot = slot_alloc(c, exc);
        if (slot == 0) { JS_FreeValue(c->ctx, exc); return -1; }
        *out_slot = slot;
        return 1;
    }
    uint32_t slot = slot_alloc(c, v);
    if (slot == 0) { JS_FreeValue(c->ctx, v); return -1; }
    *out_slot = slot;
    return 0;
}

QJS_EXPORT int32_t qjs_set_prop(uint32_t ctx_id, uint32_t obj_slot,
                                uint32_t key_ptr, uint32_t key_len,
                                uint32_t val_slot) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c || !slot_valid(c, obj_slot) || !slot_valid(c, val_slot)) return -1;

    char stack_key[256];
    bool on_heap = false;
    char *key = key_to_cstr(key_ptr, key_len, stack_key, sizeof(stack_key), &on_heap);
    if (!key) return -1;

    JSValue obj = c->slots[obj_slot].value;
    /* JS_SetPropertyStr consumes the value, so dup first — the slot still
     * owns its original reference. */
    JSValue v = JS_DupValue(c->ctx, c->slots[val_slot].value);
    int rc = JS_SetPropertyStr(c->ctx, obj, key, v);
    if (on_heap) free(key);
    if (rc < 0) {
        JSValue exc = JS_GetException(c->ctx);
        JS_FreeValue(c->ctx, exc);
        return 1;
    }
    return 0;
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

/* ------------------------------------------------------------------ */
/* Host-function bridge (§6.2, §6.3)                                   */
/* ------------------------------------------------------------------ */

/* JSCFunctionData wrapper: called by QuickJS when JS invokes a host-
 * registered function. We encode argv as a msgpack array, call out to
 * the Python host via host_call, and either return the decoded result
 * or throw the JS-side error the host produced.
 *
 * Re-entrancy: `func_data` stashes the shim-context id and the fn id.
 * The args are encoded into a fresh DynBuf rather than the per-context
 * scratch so a host function that synchronously calls back into
 * ctx.eval (which uses the scratch for its own to_msgpack) won't
 * clobber the args mid-dispatch.
 */
static JSValue shim_host_call_trampoline(JSContext *ctx, JSValueConst this_val,
                                         int argc, JSValueConst *argv,
                                         int magic, JSValueConst *func_data) {
    (void)this_val; (void)magic;

    int32_t ctx_id_val = 0, fn_id_val = 0;
    if (JS_ToInt32(ctx, &ctx_id_val, func_data[0]) < 0) return JS_EXCEPTION;
    if (JS_ToInt32(ctx, &fn_id_val, func_data[1]) < 0) return JS_EXCEPTION;
    uint32_t ctx_id = (uint32_t)ctx_id_val;
    uint32_t fn_id = (uint32_t)fn_id_val;

    ShimContext *c = ctx_lookup(ctx_id);
    if (!c) return JS_ThrowInternalError(ctx, "host shim-context vanished");

    DynBuf args;
    if (!dynbuf_init(&args, 64)) {
        return JS_ThrowOutOfMemory(ctx);
    }

    /* Encode argv as a msgpack array. */
    int32_t off = encode_array_header(&args, 0, (uint32_t)argc);
    if (off < 0) { dynbuf_free(&args); return JS_ThrowInternalError(ctx, "host arg encode failed"); }
    for (int i = 0; i < argc; i++) {
        off = encode_value(c, &args, argv[i], off, 0);
        if (off < 0) {
            dynbuf_free(&args);
            /* TODO(handles): §8 error-copy polish; for now a plain TypeError
             * keeps the JS side informed. */
            return JS_ThrowTypeError(ctx,
                "host function arg %d is not marshalable per §8", i);
        }
    }
    args.len = (uint32_t)off;

    uint32_t reply_ptr = 0, reply_len = 0;
    int32_t rc = host_call(fn_id,
                           (uint32_t)(uintptr_t)args.data, args.len,
                           &reply_ptr, &reply_len);
    dynbuf_free(&args);

    if (rc < 0) {
        return JS_ThrowInternalError(ctx, "host_call marshaling failure");
    }

    /* Decode host reply from guest memory. */
    DecCursor cur = {
        .data = (const uint8_t *)(uintptr_t)reply_ptr,
        .len = reply_len,
        .off = 0,
        .error = false,
    };
    JSValue decoded = decode_value(c, &cur, 0);
    bool decode_ok = !cur.error && !JS_IsException(decoded) && cur.off == cur.len;
    if (!decode_ok && !JS_IsException(decoded)) {
        JS_FreeValue(ctx, decoded);
    }
    /* §6.3: the shim qjs_free's the host-provided reply buffer. */
    if (reply_ptr) free((void *)(uintptr_t)reply_ptr);

    if (!decode_ok) {
        if (JS_IsException(decoded)) {
            /* propagate whatever QuickJS already set */
            return JS_EXCEPTION;
        }
        return JS_ThrowInternalError(ctx, "host reply decode failed");
    }

    if (rc == 1) {
        /* Host raised. §10.2: the reply is a JS error record
         * {name, message, stack}. Build a matching Error object whose
         * `name` property is whatever the host sent (typically "HostError"),
         * and throw it. */
        JSValue err = JS_NewError(ctx);
        if (JS_IsException(err)) { JS_FreeValue(ctx, decoded); return err; }

        /* Copy name/message/stack across from the decoded record. */
        JSValue name_v = JS_GetPropertyStr(ctx, decoded, "name");
        JSValue msg_v = JS_GetPropertyStr(ctx, decoded, "message");
        JSValue stack_v = JS_GetPropertyStr(ctx, decoded, "stack");
        JS_FreeValue(ctx, decoded);

        if (!JS_IsUndefined(name_v)) JS_SetPropertyStr(ctx, err, "name", name_v);
        else JS_FreeValue(ctx, name_v);
        if (!JS_IsUndefined(msg_v)) JS_SetPropertyStr(ctx, err, "message", msg_v);
        else JS_FreeValue(ctx, msg_v);
        if (!JS_IsUndefined(stack_v)) JS_SetPropertyStr(ctx, err, "stack", stack_v);
        else JS_FreeValue(ctx, stack_v);

        return JS_Throw(ctx, err);
    }

    return decoded;
}

QJS_EXPORT int32_t qjs_register_host_function(uint32_t ctx_id,
                                              uint32_t name_ptr, uint32_t name_len,
                                              uint32_t fn_id) {
    ShimContext *c = ctx_lookup(ctx_id);
    if (!c) return -1;

    char stack_name[256];
    bool on_heap = false;
    char *name = key_to_cstr(name_ptr, name_len, stack_name, sizeof(stack_name), &on_heap);
    if (!name) return -1;

    JSValue data[2] = {
        JS_NewInt32(c->ctx, (int32_t)ctx_id),
        JS_NewInt32(c->ctx, (int32_t)fn_id),
    };

    JSValue fn = JS_NewCFunctionData(c->ctx, shim_host_call_trampoline,
                                     /*length=*/0, /*magic=*/0,
                                     /*data_len=*/2, data);
    /* JS_NewCFunctionData dups the data values, so release our originals. */
    JS_FreeValue(c->ctx, data[0]);
    JS_FreeValue(c->ctx, data[1]);

    if (JS_IsException(fn)) {
        if (on_heap) free(name);
        JSValue exc = JS_GetException(c->ctx);
        JS_FreeValue(c->ctx, exc);
        return -1;
    }

    JSValue global = JS_GetGlobalObject(c->ctx);
    int rc = JS_SetPropertyStr(c->ctx, global, name, fn);  /* consumes fn */
    JS_FreeValue(c->ctx, global);
    if (on_heap) free(name);

    if (rc < 0) {
        JSValue exc = JS_GetException(c->ctx);
        JS_FreeValue(c->ctx, exc);
        return -1;
    }
    return 0;
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
