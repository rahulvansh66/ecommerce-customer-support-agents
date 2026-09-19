# LangGraph production patterns

Code sketches for the rules in `SKILL.md`. They are patterns, not drop-in modules:
names (`OrderTools`, `refund_agent`) are illustrative, and imports should be checked
against the installed LangGraph version. Real code in this repo must still follow
`CLAUDE.md` (full Google-style docstrings with every argument, type hints, `logging`,
block comments). The snippets below keep docstrings short to stay readable.

Contents:

1. State, reducers, input/output schemas, runtime context
2. Router with a safe default
3. Node with retry policy, compiled once with a durable checkpointer
4. Bounded loop and graceful degradation
5. Handoff between agents with a ping-pong guard
6. Human approval with `interrupt()`
7. Tool with authorization from context, not arguments
8. Idempotent side effect
9. Trimming conversation history
10. Testing with a fake model

## 1. State, reducers, input/output schemas, runtime context

```python
import operator
from dataclasses import dataclass
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import StateGraph, add_messages
from langgraph.runtime import Runtime


class InputState(TypedDict):
    """What callers may send in."""

    messages: Annotated[list[AnyMessage], add_messages]


class OutputState(TypedDict):
    """What callers get back."""

    messages: Annotated[list[AnyMessage], add_messages]


class AgentState(InputState, OutputState):
    """Internal state; keys beyond the public schemas are private to the graph."""

    schema_version: int  # bump on breaking changes, see SKILL.md section 4
    active_agent: str
    handoff_count: int
    findings: Annotated[list[str], operator.add]  # reducer: parallel-safe
    pending_action: dict | None


@dataclass(frozen=True)
class RunContext:
    """Per-run dependencies and identity. Not checkpointed, not in state."""

    tenant_id: str
    user_id: str


builder = StateGraph(
    AgentState,
    input_schema=InputState,
    output_schema=OutputState,
    context_schema=RunContext,
)


def triage(state: AgentState, runtime: Runtime[RunContext]) -> dict:
    """Read identity from runtime.context, return a partial state update."""
    user_id = runtime.context.user_id
    return {"active_agent": "triage"}
```

Invoke with identity supplied per run:

```python
graph.invoke(
    {"messages": [("user", "Where is my order?")]},
    config={"configurable": {"thread_id": thread_id}, "recursion_limit": 25},
    context=RunContext(tenant_id="t1", user_id="u1"),
)
```

## 2. Router with a safe default

```python
from typing import Literal

from langgraph.graph import END


def route_after_triage(
    state: AgentState,
) -> Literal["order_agent", "refund_agent", "human_escalation", "__end__"]:
    """Pure function of state. Reads: intent, confidence. Next: see Literal."""
    intent = state.get("intent")
    if intent == "order_status":
        return "order_agent"
    if intent == "refund":
        return "refund_agent"
    if intent == "done":
        return END
    return "human_escalation"  # default: never a dead end


builder.add_conditional_edges("triage", route_after_triage)
```

## 3. Node with retry policy, compiled once with a durable checkpointer

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import RetryPolicy
import httpx

TRANSIENT_ERRORS = (httpx.TimeoutException, httpx.ConnectError)

builder.add_node(
    "order_agent",
    order_agent_node,
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_interval=1.0,
        backoff_factor=2.0,
        jitter=True,
        retry_on=TRANSIENT_ERRORS,  # never retry bugs or validation errors
    ),
)


async def build_graph(dsn: str):
    """Compile once at startup; reuse for every request.

    Run checkpointer.setup() in a deploy/migration step in real systems.
    """
    # from_conn_string is an async context manager: keep it open for app lifetime
    # (enter it in the app lifespan handler and close on shutdown).
    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        await checkpointer.setup()
        return builder.compile(checkpointer=checkpointer)
```

In a FastAPI service, enter the saver's context in the `lifespan` function, store the
compiled graph on `app.state`, and exit the context on shutdown.

## 4. Bounded loop and graceful degradation

```python
import logging

from langgraph.errors import GraphRecursionError

logger = logging.getLogger(__name__)


async def run_turn(graph, user_input: str, thread_id: str, ctx: RunContext) -> dict:
    """Invoke with an explicit step limit and degrade instead of crashing."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 25}
    try:
        return await graph.ainvoke(
            {"messages": [("user", user_input)]}, config=config, context=ctx
        )
    except GraphRecursionError:
        logger.warning("run=turn status=recursion_limit thread_id=%s", thread_id)
        return {"messages": [("assistant", "I need a human to help with this.")]}
```

Also keep a budget in state (`steps`, `tokens_used`) and route to an exit node when
it is exceeded. The recursion limit is the last-resort backstop.

## 5. Handoff between agents with a ping-pong guard

```python
from langgraph.types import Command

MAX_HANDOFFS = 4


def handoff_node(state: AgentState) -> Command[Literal["refund_agent", "human_escalation"]]:
    """Update state and pick the next node in one step.

    Reads: handoff_count, pending_action. Writes: active_agent, handoff_count.
    Next: refund_agent or human_escalation.
    """
    if state["handoff_count"] >= MAX_HANDOFFS:
        return Command(goto="human_escalation")
    return Command(
        goto="refund_agent",
        update={
            "active_agent": "refund_agent",
            "handoff_count": state["handoff_count"] + 1,
        },
    )
```

Leaving a subgraph for a node in the parent graph:

```python
return Command(goto="supervisor", update={"findings": ["..."]}, graph=Command.PARENT)
```

Wrapping a subgraph whose schema differs from the parent, so it can evolve alone:

```python
def call_refund_team(state: AgentState) -> dict:
    """Map parent state to subgraph input and return only a concise result."""
    result = refund_subgraph.invoke({"task": state["messages"][-1].content})
    return {"findings": [result["summary"]]}  # summary, not the scratchpad
```

## 6. Human approval with `interrupt()`

```python
from langgraph.types import Command, interrupt


def approve_refund(state: AgentState) -> Command[Literal["issue_refund", "triage"]]:
    """Pause for a human decision before an irreversible action.

    Reads: pending_action. Next: issue_refund if approved, else triage.
    Everything above interrupt() re-runs on resume, so keep it cheap and idempotent.
    """
    action = state["pending_action"]
    decision = interrupt(
        {"question": "Approve refund?", "amount": action["amount"], "order": action["order_id"]}
    )
    if decision.get("approved") is True:  # validate the untrusted resume value
        return Command(goto="issue_refund")
    return Command(goto="triage", update={"pending_action": None})
```

Resume on the same thread:

```python
graph.invoke(Command(resume={"approved": True}), config={"configurable": {"thread_id": thread_id}})
```

## 7. Tool with authorization from context, not arguments

```python
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field


class OrderLookupArgs(BaseModel):
    """Arguments the model may fill in. Note: no customer or tenant ID here."""

    order_id: str = Field(description="The order number, e.g. 'A-10293'.")


@tool(args_schema=OrderLookupArgs)
def lookup_order(order_id: str, config: RunnableConfig) -> dict:
    """Look up one order's status. Use for 'where is my order'; not for refunds."""
    user_id = config["configurable"]["user_id"]  # from the server, not the model
    order = db.get_order(order_id)
    if order is None or order.owner_id != user_id:
        return {"error": "order_not_found"}  # same answer for missing and foreign
    return {"status": order.status, "eta": order.eta}  # compact, no raw row
```

`config` is injected by LangGraph and is not part of the schema the model sees.

## 8. Idempotent side effect

```python
def issue_refund(state: AgentState, config: RunnableConfig) -> dict:
    """Charge-like side effect guarded by a deterministic idempotency key."""
    thread_id = config["configurable"]["thread_id"]
    action = state["pending_action"]
    key = f"refund:{thread_id}:{action['order_id']}"  # same key on retry or resume
    payments.refund(order_id=action["order_id"], amount=action["amount"], idempotency_key=key)
    return {"pending_action": None}
```

## 9. Trimming conversation history

```python
from langchain_core.messages.utils import trim_messages


def prepare_prompt(state: AgentState) -> list[AnyMessage]:
    """Keep the model call within budget without orphaning tool results."""
    return trim_messages(
        state["messages"],
        strategy="last",
        max_tokens=6000,
        token_counter=model,  # count with the real model when possible
        start_on="human",
        include_system=True,
    )
```

For long-lived threads, prefer a summarization node that replaces old messages with a
summary (use `RemoveMessage` to delete them from state) over silent truncation.

## 10. Testing with a fake model

```python
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver


def test_unknown_intent_escalates() -> None:
    """Router default sends unknown intents to a human, no LLM involved."""
    assert route_after_triage({"intent": "gibberish"}) == "human_escalation"


def test_refund_pauses_for_approval() -> None:
    """The graph must stop at interrupt() and resume on the same thread."""
    fake = GenericFakeChatModel(messages=iter([AIMessage(content="refund")]))
    graph = build_test_graph(model=fake, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-1"}}

    graph.invoke({"messages": [("user", "refund order A-1")]}, config)
    assert graph.get_state(config).next == ("approve_refund",)

    graph.invoke(Command(resume={"approved": True}), config)
    assert graph.get_state(config).next == ()
```

Design the graph builder to accept the model and checkpointer as parameters, so tests
can inject fakes without patching.
