"""Order management agent: a single ReAct-style LangGraph graph.

Topology (the simplest that works: one agent plus tools)::

    START -> agent --(tool calls?)--> tools -> agent
                  \\--(no tool calls)--> END

* ``agent`` node: trims history, prepends the system prompt, calls the tool-bound LLM.
* ``tools`` node: prebuilt ``ToolNode`` running the order tools (errors come back as messages).
* ``route_after_agent``: pure router choosing ``tools`` or ``END``.

State is checkpointed per ``thread_id`` (= session id), so follow-up questions in
the same session keep their context. The graph is compiled once at startup and
reused for every request; :class:`OrderManagementAgent` wraps it with ``run`` and
``stream`` helpers that add correlation ids, limits and safe error handling.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models import LanguageModelLike
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    trim_messages,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.store.base import BaseStore
from pydantic import BaseModel, Field

from ecommerce_agents.agents.llm import build_chat_model_with_tools
from ecommerce_agents.agents.persistence import build_checkpointer, build_store
from ecommerce_agents.config import Settings
from ecommerce_agents.db.pool import Database
from ecommerce_agents.observability.logging import bind_log_context
from ecommerce_agents.tools.order_tools import get_order_tools

logger = logging.getLogger(__name__)

AGENT_NAME = "order_management"
FALLBACK_MESSAGE = (
    "I'm having trouble accessing our order system right now. "
    "Please try again in a few minutes or contact our support team."
)


class OrderAgentState(TypedDict):
    """Graph state, checkpointed per thread.

    Attributes:
        messages: Conversation messages; ``add_messages`` appends instead of overwriting.
        session_id: Conversation/thread id (mirrors ``thread_id``).
        customer_id: Caller's customer id ("" when unknown). Tools read the
            authoritative value from the run config, not from here.
    """

    messages: Annotated[list[AnyMessage], add_messages]
    session_id: str
    customer_id: str


class AgentResult(BaseModel):
    """Outcome of one agent turn returned to callers (API/CLI)."""

    response: str = Field(description="Final answer text for the customer.")
    session_id: str = Field(description="Session/thread id to reuse for follow-ups.")
    tool_calls: list[str] = Field(default_factory=list, description="Tools called this turn.")
    processing_time: float = Field(description="Wall-clock seconds for the turn.")
    error: bool = Field(default=False, description="True if a fallback message was returned.")


def route_after_agent(state: OrderAgentState) -> Literal["tools", "__end__"]:
    """Route after the ``agent`` node.

    Pure function of state (no I/O). Reads ``messages``; writes nothing.

    Args:
        state: Current graph state.

    Returns:
        ``"tools"`` if the last message requests tool calls, else ``"__end__"``
        (also the safe default for an empty message list).
    """
    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


def build_graph(
    model: LanguageModelLike,
    tools: Sequence[BaseTool],
    system_prompt: str,
    max_history_messages: int,
    checkpointer: BaseCheckpointSaver | None,
    store: BaseStore | None,
) -> CompiledStateGraph:
    """Assemble and compile the order management graph.

    Args:
        model: Tool-bound chat model; takes a message list, returns an ``AIMessage``.
        tools: Tools executed by the ``tools`` node.
        system_prompt: System prompt prepended on every model call.
        max_history_messages: Number of most recent messages sent to the model.
        checkpointer: Per-thread state saver (None disables persistence).
        store: Cross-thread memory store (wired for later use; no node uses it yet).

    Returns:
        The compiled graph, to be built once and reused.
    """

    async def agent_node(state: OrderAgentState) -> dict[str, list[AnyMessage]]:
        """Call the LLM with the system prompt and trimmed history.

        Reads ``messages``; writes one new ``AIMessage`` to ``messages``. On
        model failure the exception propagates to the caller, which maps it to
        a safe fallback message.

        Args:
            state: Current graph state.

        Returns:
            Partial state update containing the model's reply.
        """
        started = time.perf_counter()

        # Bound history so long conversations don't blow the context window
        history = trim_messages(
            state["messages"],
            max_tokens=max_history_messages,
            token_counter=len,
            strategy="last",
            start_on="human",
            include_system=False,
        )
        response = await model.ainvoke([SystemMessage(content=system_prompt), *history])

        usage = getattr(response, "usage_metadata", None) or {}
        logger.info(
            "node=agent status=complete tool_calls=%d input_tokens=%s output_tokens=%s "
            "duration_s=%.2f",
            len(getattr(response, "tool_calls", []) or []),
            usage.get("input_tokens", "n/a"),
            usage.get("output_tokens", "n/a"),
            time.perf_counter() - started,
        )
        return {"messages": [response]}

    workflow = StateGraph(OrderAgentState)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", ToolNode(list(tools)))
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges("agent", route_after_agent, ["tools", END])
    workflow.add_edge("tools", "agent")
    return workflow.compile(checkpointer=checkpointer, store=store, name=AGENT_NAME)


def _text(message: AnyMessage) -> str:
    """Extract plain text from a message's content.

    Args:
        message: Any LangChain message.

    Returns:
        The content as a string (text blocks joined if content is a block list).
    """
    content = message.content
    if isinstance(content, str):
        return content
    return " ".join(
        block.get("text", "") for block in content if isinstance(block, dict)
    ).strip()


def _tools_used_this_turn(messages: Sequence[AnyMessage]) -> list[str]:
    """List tool names called since the latest human message.

    Args:
        messages: Full message history of the thread.

    Returns:
        Tool names in call order.
    """
    start = 0
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            start = i
    return [
        call["name"]
        for msg in messages[start:]
        if isinstance(msg, AIMessage)
        for call in msg.tool_calls
    ]


class OrderManagementAgent:
    """Compiled order management graph plus request-level helpers."""

    def __init__(self, graph: CompiledStateGraph, settings: Settings) -> None:
        """Wrap a compiled graph.

        Args:
            graph: Graph returned by :func:`build_graph`.
            settings: Settings supplying the recursion limit.
        """
        self.graph = graph
        self._settings = settings

    @classmethod
    def create(cls, settings: Settings, db: Database) -> "OrderManagementAgent":
        """Build the agent: tools, LLM, prompt, checkpointer, store and graph.

        Args:
            settings: Resolved settings.
            db: Opened database wrapper the tools query.

        Returns:
            A ready-to-use agent.

        Raises:
            ConfigError: If required secrets or the prompt file are missing.
        """
        tools = get_order_tools(db, settings)
        model = build_chat_model_with_tools(settings, tools)
        graph = build_graph(
            model=model,
            tools=tools,
            system_prompt=settings.load_prompt(),
            max_history_messages=settings.agent.max_history_messages,
            checkpointer=build_checkpointer(settings),
            store=build_store(settings),
        )
        logger.info("component=agent status=ready name=%s tools=%d", AGENT_NAME, len(tools))
        return cls(graph, settings)

    def _run_config(self, session_id: str, customer_id: str | None, run_id: str) -> RunnableConfig:
        """Build the per-invocation config.

        ``customer_id`` goes in ``configurable`` where tools read it (trusted
        context), not in the model-visible messages.

        Args:
            session_id: Used as the LangGraph ``thread_id``.
            customer_id: Caller's customer id, or None.
            run_id: Correlation id for this invocation.

        Returns:
            Config with thread id, recursion limit, run name, tags and metadata
            (these surface in LangSmith traces).
        """
        return {
            "configurable": {"thread_id": session_id, "customer_id": customer_id or ""},
            "recursion_limit": self._settings.agent.recursion_limit,
            "run_name": AGENT_NAME,
            "tags": [AGENT_NAME, self._settings.app.env],
            "metadata": {
                "session_id": session_id,
                "run_id": run_id,
                "agent": AGENT_NAME,
                "environment": self._settings.app.env,
                "model": self._settings.llm.model,
            },
        }

    @staticmethod
    def _input(message: str, session_id: str, customer_id: str | None) -> OrderAgentState:
        """Build the graph input for one turn.

        Args:
            message: The customer's message.
            session_id: Session/thread id.
            customer_id: Caller's customer id, or None.

        Returns:
            Initial state update for this turn.
        """
        return {
            "messages": [HumanMessage(content=message)],
            "session_id": session_id,
            "customer_id": customer_id or "",
        }

    async def run(
        self, message: str, customer_id: str | None = None, session_id: str | None = None
    ) -> AgentResult:
        """Process one customer message and return the final answer.

        Failures (recursion limit, LLM or DB errors) are logged with a traceback
        and mapped to a safe fallback message; raw errors never reach the caller.

        Args:
            message: The customer's message.
            customer_id: Caller's customer id (scopes all order lookups).
            session_id: Existing session to continue; a new UUID is generated if None.

        Returns:
            The turn's result, with ``error=True`` if the fallback message was used.
        """
        session_id = session_id or str(uuid.uuid4())
        run_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()

        with bind_log_context(session_id, run_id):
            logger.info("agent=%s status=start has_customer=%s", AGENT_NAME, bool(customer_id))
            try:
                final = await self.graph.ainvoke(
                    self._input(message, session_id, customer_id),
                    config=self._run_config(session_id, customer_id, run_id),
                )
                messages = final["messages"]
                result = AgentResult(
                    response=_text(messages[-1]) or FALLBACK_MESSAGE,
                    session_id=session_id,
                    tool_calls=_tools_used_this_turn(messages),
                    processing_time=time.perf_counter() - started,
                )
            except GraphRecursionError:
                logger.warning("agent=%s status=recursion_limit_hit", AGENT_NAME)
                result = self._fallback(session_id, started)
            except Exception:
                logger.exception("agent=%s status=error", AGENT_NAME)
                result = self._fallback(session_id, started)

            logger.info(
                "agent=%s status=complete tools=%s error=%s duration_s=%.2f",
                AGENT_NAME,
                ",".join(result.tool_calls) or "none",
                result.error,
                result.processing_time,
            )
            return result

    @staticmethod
    def _fallback(session_id: str, started: float) -> AgentResult:
        """Build the safe error result.

        Args:
            session_id: Session id to echo back.
            started: ``perf_counter`` timestamp when the turn began.

        Returns:
            An ``AgentResult`` with the generic fallback message and ``error=True``.
        """
        return AgentResult(
            response=FALLBACK_MESSAGE,
            session_id=session_id,
            processing_time=time.perf_counter() - started,
            error=True,
        )

    async def stream(
        self, message: str, customer_id: str | None = None, session_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream one turn as progress and token events.

        Uses ``stream_mode=["updates", "messages"]``: ``updates`` give node
        progress (which tools were requested), ``messages`` give LLM tokens.
        Only the ``agent`` node's text tokens are emitted (tool-call fragments
        are skipped), and tool arguments/results are never streamed.

        Args:
            message: The customer's message.
            customer_id: Caller's customer id.
            session_id: Existing session to continue; a new UUID is generated if None.

        Yields:
            Event dicts: ``{"type": "progress", "node": str, "tool_calls": [str]}``,
            ``{"type": "token", "content": str}`` and, on failure,
            ``{"type": "error", "error": str}`` with a safe message.
        """
        session_id = session_id or str(uuid.uuid4())
        run_id = uuid.uuid4().hex[:12]

        with bind_log_context(session_id, run_id):
            logger.info("agent=%s status=stream_start", AGENT_NAME)
            try:
                async for mode, chunk in self.graph.astream(
                    self._input(message, session_id, customer_id),
                    config=self._run_config(session_id, customer_id, run_id),
                    stream_mode=["updates", "messages"],
                ):
                    if mode == "updates":
                        for node, update in chunk.items():
                            calls = [
                                call["name"]
                                for msg in (update or {}).get("messages", [])
                                if isinstance(msg, AIMessage)
                                for call in msg.tool_calls
                            ]
                            yield {"type": "progress", "node": node, "tool_calls": calls}
                    elif mode == "messages":
                        token, meta = chunk
                        if (
                            meta.get("langgraph_node") == "agent"
                            and isinstance(token, AIMessageChunk)
                            and token.content
                            and not token.tool_call_chunks
                        ):
                            yield {"type": "token", "content": _text(token)}
            except GraphRecursionError:
                logger.warning("agent=%s status=recursion_limit_hit", AGENT_NAME)
                yield {"type": "error", "error": FALLBACK_MESSAGE}
            except Exception:
                logger.exception("agent=%s status=stream_error", AGENT_NAME)
                yield {"type": "error", "error": FALLBACK_MESSAGE}
            else:
                logger.info("agent=%s status=stream_complete", AGENT_NAME)
