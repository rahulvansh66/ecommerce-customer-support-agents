"""Unit tests for the order agent graph using a scripted fake model (no network)."""

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from ecommerce_agents.agents.order_management_agent import (
    FALLBACK_MESSAGE,
    OrderManagementAgent,
    build_graph,
    route_after_agent,
)
from ecommerce_agents.config import Settings


@tool
async def echo_tool(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


def scripted_model(replies: Sequence[AIMessage], seen: list[int] | None = None) -> RunnableLambda:
    """Build a fake chat model returning ``replies`` in order (the last one repeats).

    Args:
        replies: Messages to return on successive calls.
        seen: Optional list that receives the length of each prompt the model saw.

    Returns:
        A runnable usable in place of a tool-bound chat model.
    """
    calls = {"n": 0}

    async def respond(messages: list[Any]) -> AIMessage:
        if seen is not None:
            seen.append(len(messages))
        # fresh copy per call: reusing one message would reuse its id and overwrite history
        reply = replies[min(calls["n"], len(replies) - 1)].model_copy(update={"id": None})
        calls["n"] += 1
        return reply

    return RunnableLambda(respond)


def make_agent(model: RunnableLambda, settings: Settings) -> OrderManagementAgent:
    """Compile the graph around a fake model and an in-memory checkpointer."""
    graph = build_graph(model, [echo_tool], "system prompt", 20, InMemorySaver(), None)
    return OrderManagementAgent(graph, settings)


def tool_call_msg() -> AIMessage:
    """An AI message asking to call ``echo_tool``."""
    return AIMessage(
        content="", tool_calls=[{"name": "echo_tool", "args": {"text": "hi"}, "id": "c1"}]
    )


def test_router_branches() -> None:
    """Router sends tool calls to ``tools`` and everything else (incl. empty) to END."""
    base = {"session_id": "s", "customer_id": ""}
    assert route_after_agent({**base, "messages": [tool_call_msg()]}) == "tools"
    assert route_after_agent({**base, "messages": [AIMessage(content="done")]}) == "__end__"
    assert route_after_agent({**base, "messages": [HumanMessage(content="hi")]}) == "__end__"
    assert route_after_agent({**base, "messages": []}) == "__end__"


async def test_tool_loop_and_final_answer(settings: Settings) -> None:
    """agent -> tools -> agent produces the final text and reports tools used."""
    agent = make_agent(scripted_model([tool_call_msg(), AIMessage(content="All done")]), settings)
    result = await agent.run("hello", customer_id="cust001")
    assert result.response == "All done"
    assert result.tool_calls == ["echo_tool"]
    assert not result.error


async def test_session_memory(settings: Settings) -> None:
    """A second turn on the same session sees the first turn's messages."""
    seen: list[int] = []
    model = scripted_model([AIMessage(content="one"), AIMessage(content="two")], seen)
    agent = make_agent(model, settings)
    first = await agent.run("q1", session_id="sess-1")
    await agent.run("q2", session_id="sess-1")
    assert first.session_id == "sess-1"
    assert seen[1] > seen[0]


async def test_recursion_limit_returns_fallback(settings: Settings) -> None:
    """A model that never stops calling tools hits the limit and yields the safe fallback."""
    agent = make_agent(scripted_model([tool_call_msg()]), settings)
    result = await agent.run("loop")
    assert result.error and result.response == FALLBACK_MESSAGE


async def test_model_exception_returns_fallback(settings: Settings) -> None:
    """LLM failures never leak raw errors to the caller."""

    async def boom(_m: Any) -> AIMessage:
        raise RuntimeError("api key abc leaked")

    agent = make_agent(RunnableLambda(boom), settings)
    result = await agent.run("hi")
    assert result.error and "leaked" not in result.response


async def test_stream_emits_progress(settings: Settings) -> None:
    """Streaming yields progress events naming the tool requested."""
    agent = make_agent(scripted_model([tool_call_msg(), AIMessage(content="ok")]), settings)
    events = [e async for e in agent.stream("hello")]
    assert any(e["type"] == "progress" and e["tool_calls"] == ["echo_tool"] for e in events)
