from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph._internal._constants import CONFIG_KEY_READ, CONFIG_KEY_SEND
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
    result: str


def _subgraph_node(state: SubgraphState) -> dict[str, str]:
    value = interrupt("quickjs needs human input")
    return {"result": value}


def _make_subgraph():
    builder = StateGraph(SubgraphState)
    builder.add_node("sub", _subgraph_node)
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


def test_quickjs_host_deferred_can_checkpoint_through_tool_interrupt_and_resume() -> None:
    subgraph = _make_subgraph()

    @tool
    def task(prompt: str) -> str:
        """Run a LangGraph subroutine for a QuickJS host task call."""
        result = subgraph.invoke({"result": ""})
        return f"{prompt} {result['result']}"

    @tool
    def eval(code: str) -> str:
        """Evaluate QuickJS code and bridge JS task(...) calls to the task tool."""
        conf = get_config()["configurable"]
        read = conf[CONFIG_KEY_READ]
        send = conf[CONFIG_KEY_SEND]
        checkpoint = read("quickjs_checkpoint")

        with Runtime() as rt:
            with rt.new_context() as ctx:

                async def js_task(prompt: str) -> str:
                    raise AssertionError(
                        "manual driver should surface task(...) as a deferred request"
                    )

                ctx.register("task", js_task)

                if checkpoint:
                    rt.restore_snapshot(Snapshot.from_bytes(checkpoint["snapshot"]), ctx)

                    # Re-enter the task tool. On resume, the subgraph interrupt
                    # inside task(...) consumes the Command(resume=...) value.
                    task_result = task.invoke({"prompt": checkpoint["prompt"]})
                    ctx._engine_ctx.resolve_pending(checkpoint["deferred_id"], task_result)
                    ctx._engine_ctx.run_pending_jobs()

                    with ctx.driver.handle_from_id(checkpoint["root_handle_id"]) as root:
                        return _promise_result_to_python(ctx, root)

                with ctx.driver.start_eval(code) as session:
                    session.run_pending_jobs()
                    requests = session.take_host_requests()
                    assert len(requests) == 1
                    request = requests[0]
                    assert request.args == ("approval?",)

                    try:
                        task_result = task.invoke({"prompt": request.args[0]})
                    except GraphInterrupt:
                        snapshot = session.create_snapshot().to_bytes()
                        send(
                            [
                                (
                                    "quickjs_checkpoint",
                                    {
                                        "snapshot": snapshot,
                                        "root_handle_id": session.root_handle_id,
                                        "deferred_id": request.deferred_id,
                                        "prompt": request.args[0],
                                    },
                                )
                            ]
                        )
                        raise

                    session.resolve(request.deferred_id, task_result)
                    session.run_pending_jobs()
                    assert session.promise_state() == "fulfilled"
                    with session.promise_result() as envelope:
                        with envelope.get("value") as value:
                            return value.to_python()

    builder = StateGraph(State)
    builder.add_node("tools", ToolNode([eval, task]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile(checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "quickjs-e2e"}}
    code = """
    const separator = "-";
    const response = await task("approval?");
    response.split("").join(separator);
    """
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

    assert "__interrupt__" in first
    persisted = graph.get_state(config).values["quickjs_checkpoint"]
    assert persisted["root_handle_id"] > 0
    assert persisted["deferred_id"] > 0
    assert persisted["prompt"] == "approval?"
    assert isinstance(persisted["snapshot"], bytes)

    interrupt_id = first["__interrupt__"][0].id
    resumed = graph.invoke(Command(resume={interrupt_id: "approved"}), config=config)

    tool_messages = [m for m in resumed["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].name == "eval"
    assert tool_messages[0].content == "a-p-p-r-o-v-a-l-?- -a-p-p-r-o-v-e-d"
    assert (
        graph.get_state(config).values["quickjs_checkpoint"]["deferred_id"]
        == persisted["deferred_id"]
    )
