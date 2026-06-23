from __future__ import annotations

import time
from typing import Annotated, Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph._internal._constants import CONFIG_KEY_READ, CONFIG_KEY_SCRATCHPAD, CONFIG_KEY_SEND
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from quickjs_rs import Handle, Runtime, Snapshot


class SubgraphState(TypedDict):
    prompt: str
    result: str


def _make_subgraph(events: list[tuple[str, float]]):
    def subgraph_node(state: SubgraphState) -> dict[str, str]:
        prompt = state["prompt"]
        if prompt.startswith("auto:"):
            events.append(("auto:start", time.monotonic()))
            time.sleep(0.05)
            events.append(("auto:done", time.monotonic()))
            return {"result": f"done:{prompt}"}

        value = interrupt({"prompt": prompt})
        return {"result": f"human:{prompt}:{value}"}

    builder = StateGraph(SubgraphState)
    builder.add_node("sub", subgraph_node)
    builder.add_edge(START, "sub")
    builder.add_edge("sub", END)
    return builder.compile()


def _promise_result_to_python(ctx: Any, promise: Handle) -> Any:
    assert ctx._engine_ctx.promise_state(promise._require_live()) == 1
    with Handle(ctx, ctx._engine_ctx.promise_result(promise._require_live())) as result:
        value = result.to_python()
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


class State(TypedDict):
    messages: Annotated[list, add_messages]
    quickjs_checkpoint: dict[str, Any]


def test_quickjs_collects_multiple_host_interrupts_before_yielding_once() -> None:
    events: list[tuple[str, float]] = []
    subgraph = _make_subgraph(events)

    @tool
    def task(prompt: str, call_id: str) -> str:
        """Host task delegates conditional auto/HITL behavior to the subgraph."""
        config = get_config()
        subgraph_configurable = {
            **config["configurable"],
            "checkpoint_ns": f"quickjs_host_call_{call_id}",
        }
        # This call is an independent host-call continuation. Drop the parent
        # scratchpad so LangGraph does not append the parent subgraph counter to
        # our explicit checkpoint namespace; the parent resume map/checkpointer
        # still flow through the rest of the config.
        subgraph_configurable.pop(CONFIG_KEY_SCRATCHPAD, None)
        result = subgraph.invoke(
            {"prompt": prompt, "result": ""},
            config={**config, "configurable": subgraph_configurable},
        )
        if interrupts := result.get("__interrupt__"):
            raise GraphInterrupt(tuple(interrupts))
        return result["result"]

    @tool
    def eval(code: str) -> str:
        """Evaluate QuickJS and bridge all JS task(...) calls to task tool."""
        conf = get_config()["configurable"]
        read = conf[CONFIG_KEY_READ]
        send = conf[CONFIG_KEY_SEND]
        checkpoint = read("quickjs_checkpoint")

        with Runtime() as rt:
            with rt.new_context() as ctx:

                async def js_task(prompt: str) -> str:
                    raise AssertionError("manual driver should capture deferred host calls")

                ctx.register("task", js_task)

                if checkpoint:
                    rt.restore_snapshot(Snapshot.from_bytes(checkpoint["snapshot"]), ctx)
                    for call in checkpoint["parked_calls"]:
                        result = task.invoke({"prompt": call["prompt"], "call_id": call["call_id"]})
                        ctx._engine_ctx.resolve_pending(call["deferred_id"], result)
                        ctx._engine_ctx.run_pending_jobs()

                    with ctx.driver.handle_from_id(checkpoint["root_handle_id"]) as root:
                        return _promise_result_to_python(ctx, root)

                with ctx.driver.start_eval(code) as session:
                    session.run_pending_jobs()
                    requests = list(session.take_host_requests())
                    assert [request.args[0] for request in requests] == [
                        "human:a",
                        "auto:b",
                        "human:c",
                    ]

                    parked_calls: list[dict[str, Any]] = []
                    interrupts: list[Any] = []
                    for request in requests:
                        prompt = request.args[0]
                        call_id = str(request.deferred_id)
                        try:
                            result = task.invoke({"prompt": prompt, "call_id": call_id})
                        except GraphInterrupt as exc:
                            parked_calls.append(
                                {
                                    "deferred_id": request.deferred_id,
                                    "prompt": prompt,
                                    "call_id": call_id,
                                }
                            )
                            events.append((f"park:{prompt}", time.monotonic()))
                            interrupts.extend(exc.args[0])
                            continue

                        session.resolve(request.deferred_id, result)
                        session.run_pending_jobs()

                    assert parked_calls
                    assert len(interrupts) == 2
                    assert session.promise_state() == "pending"

                    events.append(("raise:interrupts", time.monotonic()))
                    send(
                        [
                            (
                                "quickjs_checkpoint",
                                {
                                    "snapshot": session.create_snapshot().to_bytes(),
                                    "root_handle_id": session.root_handle_id,
                                    "parked_calls": parked_calls,
                                },
                            )
                        ]
                    )
                    raise GraphInterrupt(tuple(interrupts))

    builder = StateGraph(State)
    builder.add_node("tools", ToolNode([eval, task]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile(checkpointer=MemorySaver())

    code = """
    const [a, b, c] = await Promise.all([
        task("human:a"),
        task("auto:b"),
        task("human:c"),
    ]);
    [a, b, c].join("|");
    """
    config = {"configurable": {"thread_id": "quickjs-multi-interrupt-e2e"}}
    first = graph.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "tool-1", "name": "eval", "args": {"code": code}}],
                )
            ],
            "quickjs_checkpoint": {},
        },
        config=config,
    )

    interrupts = first["__interrupt__"]
    assert len(interrupts) == 2
    assert [interrupt.value for interrupt in interrupts] == [
        {"prompt": "human:a"},
        {"prompt": "human:c"},
    ]
    state_after_first = graph.get_state(config)
    assert state_after_first.next == ("tools",)
    checkpoint = state_after_first.values["quickjs_checkpoint"]
    assert [call["prompt"] for call in checkpoint["parked_calls"]] == ["human:a", "human:c"]
    event_times = dict(events)
    assert event_times["park:human:a"] < event_times["auto:start"]
    assert event_times["auto:start"] < event_times["auto:done"]
    assert event_times["auto:done"] < event_times["park:human:c"]
    assert event_times["auto:done"] < event_times["raise:interrupts"]
    assert event_times["auto:done"] - event_times["auto:start"] >= 0.04

    resumed = graph.invoke(
        Command(
            resume={
                interrupts[0].id: "answer-a",
                interrupts[1].id: "answer-c",
            }
        ),
        config=config,
    )

    tool_messages = [message for message in resumed["messages"] if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].name == "eval"
    assert tool_messages[0].content == "human:human:a:answer-a|done:auto:b|human:human:c:answer-c"
    print(event_times)
