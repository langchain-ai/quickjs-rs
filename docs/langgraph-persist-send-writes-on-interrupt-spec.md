# LangGraph spec: persist pending task writes when flushing interrupts

Status: proposal for LangGraph implementation

## Summary

Change LangGraph's `GraphInterrupt` commit path so that pending task writes emitted before a controlled interrupt are persisted into the checkpoint, while the interrupt itself is still recorded and the interrupted task remains resumable.

This lets code do:

```python
from langgraph.config import get_config
from langgraph._internal._constants import CONFIG_KEY_SEND
from langgraph.types import interrupt


def node(state):
    send = get_config()["configurable"][CONFIG_KEY_SEND]

    # Persist continuation/checkpoint state before yielding an interrupt.
    send([
        ("quickjs_checkpoint", {"snapshot": "...", "parked": {...}}),
    ])

    resume = interrupt({"type": "quickjs_hitl", "items": [...]})
    ...
```

Expected after the interrupted invocation:

```python
snapshot = graph.get_state(config)
snapshot.values["quickjs_checkpoint"] == {"snapshot": "...", "parked": {...}}
snapshot.next == ("node",)  # interrupted task remains resumable
```

No new user-facing API is required. The core semantic change is:

> A `GraphInterrupt` is a controlled suspension point, not a task failure. Writes emitted before the suspension point should be checkpointed rather than dropped.

## Motivation

Some runtimes need to persist continuation state immediately before yielding a HITL interrupt. Example: a QuickJS runtime may:

1. drive JS until several host `task(...)` calls interrupt;
2. park their JS deferred promises;
3. snapshot the QuickJS VM once;
4. write that snapshot/manifest into graph state;
5. yield one aggregate LangGraph interrupt.

On resume, the interrupted node/tool re-executes from the top. It must be able to read the QuickJS snapshot from graph state **before** re-entering task/subgraph execution.

Using a separate store works, but the graph checkpoint already has the right durability and lifecycle. We want the checkpoint state to include the snapshot before the interrupt is yielded.

## Current behavior

Pregel `send` is injected into task config as `writes.extend`.

Relevant source:

```python
# langgraph/pregel/_algo.py
CONFIG_KEY_SEND: writes.extend
```

So this:

```python
send([("quickjs_checkpoint", checkpoint)])
```

appends to the current task's in-memory `task.writes`.

However, on `GraphInterrupt`, `PregelRunner.commit(...)` persists only the interrupt payload and resume writes:

```python
# langgraph/pregel/_runner.py
elif exception:
    if isinstance(exception, GraphInterrupt):
        # save interrupt to checkpointer
        if exception.args[0]:
            writes = [(INTERRUPT, exception.args[0])]
            if resumes := [w for w in task.writes if w[0] == RESUME]:
                writes.extend(resumes)
            self.put_writes()(task.id, writes)
```

It does **not** persist the other pending `task.writes`. Those writes may appear in immediate streamed/invoke output, but are not visible in `graph.get_state(config).values` after the interrupt.

That causes this concrete failure mode:

```text
send checkpoint write
→ interrupt
→ checkpoint write is dropped
→ resume re-executes task
→ task cannot read checkpoint state it intentionally wrote before interrupting
```

## Proposed behavior

When `PregelRunner.commit(...)` handles `GraphInterrupt`, it should persist the task's pending writes before/alongside recording the interrupt.

The implementation must preserve two properties:

1. pending writes are visible in checkpoint state after the interrupted invocation;
2. the interrupted task remains pending/resumable.

Manual testing showed that writing pending checkpoint updates under the interrupted task id can make the task appear completed or otherwise affect resume behavior. Writing with `NULL_TASK_ID` made `graph.get_state(config).values` include the update while preserving the interrupted task.

So the recommended implementation is:

```text
persist task.writes as checkpoint writes under NULL_TASK_ID or equivalent neutral identity
persist INTERRUPT/RESUME under the interrupted task id as today
```

### Conceptual commit flow

```python
if isinstance(exception, GraphInterrupt):
    if exception.args[0]:
        if task.writes:
            # Persist writes produced before the suspension point without marking
            # the interrupted task as completed.
            self.put_writes()(NULL_TASK_ID, list(task.writes))

        interrupt_writes = [(INTERRUPT, exception.args[0])]

        if resumes := [w for w in task.writes if w[0] == RESUME]:
            interrupt_writes.extend(resumes)

        self.put_writes()(task.id, interrupt_writes)
```

An equivalent internal mechanism is fine. The semantic requirement is stronger than the exact task id: **do not drop pending task writes on interrupt, and do not complete the interrupted task.**

## Why not filter pending writes?

The desired semantic is that writes emitted before a controlled suspension point are durable. If a task explicitly writes before interrupting, dropping that write violates program order:

```python
send([("quickjs_checkpoint", checkpoint)])
interrupt(payload)
```

This should mean:

```text
1. checkpoint this state
2. yield interrupt
```

not:

```text
1. buffer this state transiently
2. yield interrupt and discard the state
```

The current behavior makes it impossible for a node/tool to persist continuation state immediately before yielding an interrupt without using a separate store or checkpointer internals.

Concerns about duplicate reducer writes on resume are real, but they are consequences of explicit writes before a suspension point. They should be documented and handled through idempotency/guards where needed, not by silently dropping writes.

## Required semantics

Given:

```python
class State(TypedDict):
    quickjs_checkpoint: dict
    result: str


def node(state):
    send = get_config()["configurable"][CONFIG_KEY_SEND]
    send([("quickjs_checkpoint", {"snapshot": "abc"})])
    resume = interrupt("trip")
    return {"result": resume}
```

After first invocation:

```python
first = graph.invoke({"quickjs_checkpoint": {}, "result": ""}, config)
state = graph.get_state(config)
```

Expected:

```python
"__interrupt__" in first
state.values["quickjs_checkpoint"] == {"snapshot": "abc"}
state.next == ("node",)  # or equivalent pending interrupted task
state.tasks[0].interrupts  # contains interrupt
```

On resume:

```python
second = graph.invoke(Command(resume="ok"), config)
```

Expected:

```python
second["quickjs_checkpoint"] == {"snapshot": "abc"}
second["result"] == "ok"
graph.get_state(config).values["quickjs_checkpoint"] == {"snapshot": "abc"}
```

The interrupted task must re-execute/resume normally.

## Why use `send` rather than `interrupt(..., update=...)`

The QuickJS/HITL flow may collect any number of interrupted host tasks before yielding a single aggregate graph interrupt.

The desired flow is:

```python
parked = collect_parked_hitl_tasks()
snapshot = create_quickjs_snapshot(parked)

send([("quickjs_checkpoint", {"snapshot": snapshot, "parked": parked})])

interrupt({
    "type": "quickjs_hitl",
    "items": [p.interrupt_payload for p in parked.values()],
})
```

Attaching an `update` to a single `interrupt(...)` call is awkward because the checkpoint update is not semantically owned by one interrupt. It is the aggregate continuation state for the whole suspended execution.

Persisting pending `send` writes on interrupt matches the actual control flow while avoiding a new public API.

## Implementation notes

Likely file:

- `langgraph/pregel/_runner.py`

Current `GraphInterrupt` branch should be changed to persist pending task writes separately from the interrupt write, preserving resumability.

Potential additional import/constant:

- `NULL_TASK_ID`

Potential pseudo-patch:

```python
elif exception:
    if isinstance(exception, GraphInterrupt):
        if exception.args[0]:
            if task.writes:
                self.put_writes()(NULL_TASK_ID, list(task.writes))

            writes = [(INTERRUPT, exception.args[0])]
            if resumes := [w for w in task.writes if w[0] == RESUME]:
                writes.extend(resumes)
            self.put_writes()(task.id, writes)
```

If writing via `NULL_TASK_ID` from `_runner.py` is insufficient because `loop.put_writes` handles `NULL_TASK_ID` specially, keep/extend that behavior. In `PregelLoop.put_writes(...)`, `NULL_TASK_ID` writes are accumulated and persisted without associating them with task completion.

Relevant source:

```python
# langgraph/pregel/_loop.py
if task_id == NULL_TASK_ID:
    # writes for the null task are accumulated
    ...
else:
    # remove existing writes for this task
    ...
```

This is the behavior we want.

## Tests

### 1. Pending write persists on interrupt

```python
class State(TypedDict):
    quickjs_checkpoint: dict
    result: str


def node(state):
    send = get_config()["configurable"][CONFIG_KEY_SEND]
    send([("quickjs_checkpoint", {"snapshot": "abc"})])
    value = interrupt("trip")
    return {"result": value}

builder = StateGraph(State)
builder.add_node("node", node)
builder.add_edge(START, "node")
builder.add_edge("node", END)
graph = builder.compile(checkpointer=MemorySaver())
config = {"configurable": {"thread_id": "t"}}

first = graph.invoke({"quickjs_checkpoint": {}, "result": ""}, config)
assert "__interrupt__" in first

snapshot = graph.get_state(config)
assert snapshot.values["quickjs_checkpoint"] == {"snapshot": "abc"}
assert snapshot.next == ("node",)

second = graph.invoke(Command(resume="ok"), config)
assert second["quickjs_checkpoint"] == {"snapshot": "abc"}
assert second["result"] == "ok"
```

### 2. Interrupted task remains resumable

Same test should assert the node actually resumes and returns the resume value.

### 3. Interrupt/resume metadata remains correct

Ensure `INTERRUPT` and `RESUME` are still persisted so `Command(resume=...)` works exactly as before.

### 4. Reducer channel behavior

Test a reducer channel, e.g. `add_messages`, to define/document that writes emitted before interrupt are durable and may need idempotency guards if the node emits them again on resume.

### 5. Tool/subgraph flow

Test from inside a `ToolNode` wrapping a tool that invokes a subgraph which interrupts:

```python
@tool
def task():
    try:
        return subgraph.invoke(...)
    except GraphInterrupt:
        send([("quickjs_checkpoint", checkpoint)])
        raise
```

After first parent graph invoke:

```python
graph.get_state(config).values["quickjs_checkpoint"] == checkpoint
```

Then resume:

```python
graph.invoke(Command(resume=...), config)
```

The tool/subgraph resumes and the checkpoint value remains available.

## Acceptance criteria

Implementation is complete when:

1. Pending task writes are persisted when a task exits with `GraphInterrupt`.
2. These writes are visible in `graph.get_state(config).values` immediately after the interrupted invocation.
3. The interrupted task remains pending/resumable.
4. `Command(resume=...)` resumes the interrupted task normally.
5. Interrupt and resume metadata continue to work correctly.
6. Existing code that does not write before interrupt behaves unchanged.
7. Tests cover simple node and ToolNode/subgraph cases.
