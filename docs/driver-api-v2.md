# QuickJS Driver API v2 Sketch

Status: draft / design notes

This document sketches a v2-shaped execution driver API for `quickjs-rs` / `quickjs-wasm`. It is intentionally written as a spec we can iterate on, not as committed public API.

## Motivation

The current v1 API is centered around high-level calls:

```python
ctx.eval(...)
await ctx.eval_async(...)
ctx.register(...)
```

That API drives execution to completion and tightly couples async host calls to Python `asyncio.Task`s. This works for ordinary async host functions, but it is too opinionated for host-in-the-loop (HITL) flows where a host call may intentionally park a pending JS promise, snapshot the guest heap, and resume later with external input.

The wasm engine already has lower-level primitives:

- host native calls return an `i32` guest handle;
- async/deferred host calls can return a handle to a JS `Promise`;
- deferred promise resolvers are keyed by `deferred_id`;
- the host can resolve/reject a deferred later;
- QuickJS jobs can be pumped explicitly;
- promise state/result can be inspected;
- whole-memory snapshots preserve pending promises and deferred resolver state.

The v2 driver API should expose these mechanics directly while leaving policy to an embedding layer.

## Goals

1. Decouple guest execution from host execution policy.
2. Make host calls produce generic host requests, not immediately scheduled Python tasks.
3. Support both direct sync host calls and promise/deferred host calls in the same context.
4. Allow custom drivers to run, await, reject, park, snapshot, and resume host requests.
5. Preserve current v1 behavior as sugar/default policy.
6. Provide a mechanical basis for HITL checkpoint/resume without baking LangGraph semantics into `quickjs-rs`.

## Non-goals

- Do not make `quickjs-rs` understand LangGraph interrupts.
- Do not make normal `ctx.eval()` silently await promises.
- Do not snapshot arbitrary in-flight Python tasks.
- Do not make parked sync/native stack frames checkpointable.
- Do not require JS-wrapper desugaring for native host bindings, though higher layers may choose to generate wrappers.

## Core concepts

### Host boundary

A registered host callable has a JS boundary contract independent of the Python callable shape.

```python
ctx.register("add", add, boundary="sync")
ctx.register("task", task, boundary="promise")
```

Possible boundary kinds:

| Boundary | JS sees | Checkpointable | Host result target |
| --- | --- | --- | --- |
| `sync` | plain value / thrown error | no | current native host-call return |
| `promise` | `Promise` | yes, if parked and idle | deferred promise resolver |

A sync Python implementation may be exposed through a promise boundary:

```python
def cached_lookup(key: str) -> str:
    return cache[key]

ctx.register("cachedLookup", cached_lookup, boundary="promise")
```

JS must still use it as a promise:

```js
const value = await cachedLookup("x");
```

### Host request

A promise-boundary host call creates a host request and returns a Promise handle to QuickJS.

```python
@dataclass(frozen=True)
class HostRequest:
    request_id: int
    deferred_id: int
    fn_id: int
    name: str
    args: tuple[Any, ...]
    boundary: Literal["promise"]
```

`request_id` and `deferred_id` may initially be the same value. The important stable key for promise settlement is `deferred_id`.

### Deferred id

`deferred_id` identifies a pending guest promise resolver/rejector pair inside the wasm guest. For promise-boundary calls:

```text
JS native host call
→ create deferred Promise
→ enqueue HostRequest(deferred_id, fn_id, args)
→ return Promise handle to JS
```

Later:

```python
ctx.resolve_deferred(deferred_id, value)
ctx.reject_deferred(deferred_id, error)
```

### Driver

A driver owns policy:

- which host requests to run;
- whether to run them sync or async;
- whether to park them;
- whether pending root promises are deadlocks or valid suspension points;
- when to snapshot;
- how to resume.

The default driver preserves v1 behavior. A LangChain driver can implement HITL.

### Root promise

For async/promise evaluation, the driver needs a root value to drive. Usually this is a top-level Promise handle.

Possible root forms:

1. A Python `Handle` / `QjsHandle` to a Promise returned by the start call.
2. A JS global root promise, e.g. `globalThis.__quickjs_root`, useful across snapshot/restore.
3. A checkpoint-stored raw guest handle id, advanced/internal only.

The JS-global-root approach is likely safest for checkpoint APIs.

## Execution modes

### Sync eval

Current v1-compatible sync eval remains stack-shaped:

```text
ctx.eval(...)
→ normal QuickJS eval
→ sync-boundary host calls execute directly
→ returns final JS value or raises
```

If sync eval calls a promise-boundary host function, default behavior should remain an error unless the caller explicitly opts into receiving an opaque Promise handle through a lower-level API.

### Promise eval / async eval

Promise eval starts JS execution and returns a Promise handle. A driver then drives the Promise.

Sources of a root Promise may include:

- QuickJS async eval / `eval_promise` / `JS_EVAL_FLAG_ASYNC`;
- normal eval of an async IIFE;
- normal eval returning an existing Promise handle;
- module evaluation promise, once module snapshots are supported.

Example harness-controlled code:

```js
globalThis.__quickjs_root = (async () => {
  const result = await task({ input: "hello" });
  return result;
})();
```

The driver drives `globalThis.__quickjs_root`.

## Proposed API surface

Names are placeholders.

### Registration

```python
ctx.register(
    name: str,
    fn: Callable[..., Any],
    *,
    boundary: Literal["sync", "promise"] | None = None,
    is_async: bool | None = None,
) -> None
```

Default compatibility policy:

- `boundary=None` preserves v1 inference.
- sync functions default to `boundary="sync"`.
- async functions default to `boundary="promise"`.
- `is_async` remains a callable implementation hint, not the JS boundary in the long term.

Long-term v2 policy may require explicit `boundary=` for clarity.

### Starting execution

```python
root = ctx.driver.start_eval(
    code: str,
    *,
    mode: Literal["normal", "promise"] = "promise",
    module: bool = False,
    filename: str = "<eval>",
    transform_flags: TransformFlagsProvider | None = None,
) -> Handle
```

Semantics:

- `mode="normal"` runs normal QuickJS eval and returns the resulting handle.
- `mode="promise"` returns a root Promise handle.
- The call does not drive host requests to completion.

Alternative names:

```python
ctx.start_eval(...)
ctx.eval_start(...)
ctx.eval_handle_start(...)
```

### Driver step

```python
state = ctx.driver.step(root: Handle) -> DriveState
```

A step should:

1. drain QuickJS pending jobs;
2. collect newly emitted host requests;
3. inspect root state if root is a Promise;
4. return a structured state.

Possible states:

```python
@dataclass(frozen=True)
class DriveFulfilled:
    value: Handle

@dataclass(frozen=True)
class DriveRejected:
    reason: Handle

@dataclass(frozen=True)
class DriveRequests:
    requests: tuple[HostRequest, ...]

@dataclass(frozen=True)
class DriveBlocked:
    root: Handle
    pending_deferred_ids: frozenset[int]
    parked_deferred_ids: frozenset[int]

DriveState = DriveFulfilled | DriveRejected | DriveRequests | DriveBlocked
```

A single step may both observe host requests and find the root pending. Returning requests first is usually preferable so policy code can process them before declaring blocked.

### Host request queue

```python
requests = ctx.driver.take_host_requests() -> tuple[HostRequest, ...]
```

Requests are emitted by promise-boundary host calls. Taking requests transfers them to the driver. The engine does not schedule Python tasks by itself.

Open question: should `step()` automatically include/take requests, or should request draining be explicit only?

### Deferred settlement

```python
ctx.driver.resolve_deferred(deferred_id: int, value: Any) -> None
ctx.driver.reject_deferred(
    deferred_id: int,
    error: BaseException | Any,
    *,
    name: str | None = None,
    message: str | None = None,
    stack: str | None = None,
) -> None
```

Semantics:

- Settlement is single-use.
- Unknown or already-settled ids raise `QuickJSError` / `InvalidDeferredError`.
- After settlement, the caller should pump pending jobs before expecting JS continuations to run.

### Promise inspection

```python
ctx.driver.promise_state(handle: Handle) -> Literal["pending", "fulfilled", "rejected"]
ctx.driver.promise_result(handle: Handle) -> Handle
ctx.driver.run_pending_jobs() -> int
```

These likely wrap existing internal primitives.

### Driving helpers

Default helper:

```python
result = await ctx.driver.drive_to_completion(root: Handle) -> Handle
```

Manual helper:

```python
state = await ctx.driver.drive_until_blocked(root: Handle) -> DriveState
```

The default v1-compatible driver treats:

```text
root pending + no running host work + no queued requests
```

as `DeadlockError`.

A custom driver may treat:

```text
root pending + parked deferred ids
```

as a checkpointable suspension.

## Host-call trampoline behavior

### Sync boundary

```text
_host_call(name, args)
→ lookup fn_id
→ dispatch sync HostRequest or call default sync handler
→ marshal result to guest handle
→ return value handle
```

For v1 compatibility, the default sync handler may still call the Python function inline.

### Promise boundary

```text
_host_call(name, args)
→ lookup fn_id
→ promise_handle, deferred_id = new_deferred()
→ enqueue HostRequest(deferred_id, fn_id, name, args)
→ return promise_handle
```

No Python task is scheduled by `_host_call` itself.

## Default v1-compatible driver

The default driver should reproduce current `eval_async()` behavior:

```python
async def drive_to_completion(root: Handle) -> Handle:
    running: dict[int, asyncio.Task[Any]] = {}

    while True:
        jobs = ctx.driver.run_pending_jobs()

        for req in ctx.driver.take_host_requests():
            running[req.deferred_id] = asyncio.create_task(run_request(req))

        state = ctx.driver.promise_state(root)

        if state == "fulfilled":
            return ctx.driver.promise_result(root)

        if state == "rejected":
            reason = ctx.driver.promise_result(root)
            raise_from_reason(reason)

        if not running:
            raise DeadlockError("root promise pending but no host work is running")

        done, _ = await wait_for_one(running.values())
        for task in done:
            req = request_for_task(task)
            try:
                value = task.result()
            except BaseException as exc:
                ctx.driver.reject_deferred(req.deferred_id, exc)
            else:
                ctx.driver.resolve_deferred(req.deferred_id, value)
            finally:
                running.pop(req.deferred_id, None)
```

Important: real implementation must preserve current cancellation and timeout semantics.

## HITL driver sketch

```python
async def drive_langchain(root: Handle) -> Any:
    running: dict[int, asyncio.Task[Any]] = {}
    parked: dict[int, InterruptRecord] = {}

    while True:
        ctx.driver.run_pending_jobs()

        for req in ctx.driver.take_host_requests():
            running[req.deferred_id] = asyncio.create_task(run_subroutine(req))

        state = ctx.driver.promise_state(root)
        if state == "fulfilled":
            return ctx.driver.promise_result(root)
        if state == "rejected":
            raise_from_reason(ctx.driver.promise_result(root))

        if running:
            done = await wait_for_some(running.values())
            for task in done:
                req = request_for_task(task)
                try:
                    value = task.result()
                except GraphInterrupt as interrupt:
                    parked[req.deferred_id] = InterruptRecord(req, interrupt)
                except BaseException as exc:
                    ctx.driver.reject_deferred(req.deferred_id, exc)
                else:
                    ctx.driver.resolve_deferred(req.deferred_id, value)
                finally:
                    running.pop(req.deferred_id, None)
            continue

        if parked:
            checkpoint = ctx.driver.snapshot_blocked(
                root=root,
                parked_deferred_ids=set(parked),
            )
            raise SuspendedInterrupt(checkpoint, parked)

        raise DeadlockError("root promise pending with no work and no parked interrupts")
```

## Snapshot/checkpoint API

### Blocked snapshot

```python
checkpoint = ctx.driver.snapshot_blocked(
    *,
    root: Handle | str,
    parked_deferred_ids: set[int],
    metadata: Mapping[str, Any] | None = None,
) -> DriverCheckpoint
```

`root` may be a handle or a JS global locator such as `"globalThis.__quickjs_root"`.

Required assertions:

- context is not closed;
- QuickJS is not currently executing;
- no Python host task is running under the driver;
- request queue is empty or accounted for;
- root Promise is pending, unless snapshotting completed state intentionally;
- each `parked_deferred_id` is known and unresolved;
- module-mode snapshots remain unsupported until module snapshot support lands.

Checkpoint shape:

```python
@dataclass(frozen=True)
class DriverCheckpoint:
    snapshot: Snapshot
    root: RootLocator
    parked_deferred_ids: frozenset[int]
    metadata: Mapping[str, Any]
```

### Resume

```python
session = ctx.driver.restore_checkpoint(checkpoint)

for deferred_id, value in resume_values.items():
    session.resolve_deferred(deferred_id, value)

result = await session.drive_to_completion()
```

Host bindings must be reinstalled compatibly before or during restore. Snapshots do not capture Python host registries.

## Root locators

Open design question: how should root promises survive restore?

Options:

### Raw handle id

Store raw guest handle id in checkpoint.

Pros:

- direct and efficient;
- matches current internal representation.

Cons:

- easy to misuse;
- Python `Handle` objects cannot cross contexts;
- raw pointer-like ids are not a friendly public API.

### JS global root

Require harness to root the promise in JS:

```js
globalThis.__quickjs_root = (async () => { ... })();
```

Checkpoint stores the global path.

Pros:

- public-friendly;
- restore can look up the promise by name;
- avoids exposing raw handle ids.

Cons:

- requires harness convention;
- global naming/collision policy required.

Recommendation: use JS global root for public checkpoint APIs; allow raw handle ids only in internal/experimental APIs.

## Cancellation and timeout semantics

The default driver must preserve current semantics:

- cancellation rejects pending deferred promises with `HostCancellationError` before tearing down host tasks;
- JS catch/finally handlers get a chance to run via pending-job drain;
- if JS absorbs cancellation, return/raise according to current behavior;
- timeout interrupts hot JS loops and cancels/settles host work consistently.

Custom drivers should have access to primitives to implement equivalent policies, but `quickjs-rs` should not force one policy beyond safety checks.

## Error handling

Sync-boundary host errors preserve current behavior:

- JS-visible error is sanitized as `HostError: Host function failed`;
- Python eval boundary may re-raise original exception through side channel.

Promise-boundary host errors:

- default driver rejects deferred with sanitized `HostError`;
- original Python exception may be retained host-side for boundary re-raise if the root Promise rejection escapes;
- custom drivers may reject with structured JS errors, subject to existing no-leak policy.

Open question: how should original-exception side channels work across checkpoint/restore? Likely they should not; checkpointed rejections should be serialized explicitly or sanitized.

## Mixing sync and promise boundaries

Same context may register both kinds.

Rules:

| Eval / driver mode | sync-boundary calls | promise-boundary calls |
| --- | --- | --- |
| `ctx.eval()` v1 sync | allowed | error by default |
| `ctx.eval_handle()` raw normal | allowed | returns Promise if caller can handle it |
| promise/driver eval | allowed | allowed |

Sync-boundary calls inside promise/driver eval still execute inline and can block the driver while QuickJS is on the stack. Long-running or interruptible work should use promise boundary.

## Safety invariants

1. A promise-boundary host call must return its Promise handle to QuickJS before it can be parked/checkpointed.
2. A sync-boundary host call cannot be parked across snapshot because QuickJS is mid-native-call.
3. The driver must not snapshot while Python host tasks are running.
4. The driver must not snapshot while QuickJS is actively executing.
5. A parked deferred must remain unresolved in the guest heap.
6. Deferred settlement is single-use.
7. Restored contexts require compatible host bindings.
8. Snapshot bytes remain trusted input.

## Internal implementation plan

### Phase 1: Host request queue

- Add a host request queue to the Python engine/context layer.
- Change async/promise host dispatch from immediate task scheduling to enqueueing `HostRequest`.
- Keep default `eval_async()` behavior by having the default driver consume the queue and schedule tasks.

### Phase 2: Driver primitives

Expose provisional APIs for:

- start promise eval;
- take host requests;
- resolve/reject deferred;
- run pending jobs;
- inspect promise state/result.

### Phase 3: Blocked snapshot

Add `snapshot_blocked(...)` with safety assertions for parked deferreds.

### Phase 4: LangChain HITL proof of concept

Implement custom driver in `langchain-quickjs`:

- promise-boundary `task` / `ptc`;
- park deferred on interrupt;
- snapshot blocked state;
- restore and resolve with resume value;
- continue root promise.

### Phase 5: Public v2 API

After the HITL proof is stable, design a final session/driver public API and deprecate ambiguous v1 concepts.

## Open questions

1. Should `step()` consume host requests or should requests always be taken explicitly?
2. Should v2 require explicit `boundary=` on registration?
3. Should `is_async` remain as a public argument or become an implementation detail?
4. Should raw handle ids be exposed at all?
5. How should root promises be represented across checkpoint/restore?
6. How should host binding compatibility be validated on restore?
7. Can module-mode snapshots be supported, or must driver checkpoints require script mode?
8. What is the right public namespace: `ctx.driver`, `ctx.raw`, `ctx.experimental`, or `ExecutionSession`?
9. How should original Python exception side channels behave across checkpoints?
10. Should sync eval have an opt-in mode that allows returning opaque Promise handles instead of raising?

## Terminology preference

Use `boundary="promise"` rather than `is_async=True` for JS-visible behavior.

Use `deferred_id` only for promise-boundary settlement.

Use `HostRequest` for the generic host/guest coordination event.

Use `driver` or `session` for the policy layer that consumes requests and drives root promises.
