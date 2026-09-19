"""FastAPI service for the order management agent.

Run with ``uv run uvicorn ecommerce_agents.api.app:app`` (or ``uv run order-agent``).
Handlers stay thin: validate input, resolve the session id, call the agent, and
translate the result. All business logic lives in the graph.

Endpoints: ``GET /health``, ``POST /process``, ``POST /process/stream`` (NDJSON).
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from ecommerce_agents.agents.order_management_agent import AGENT_NAME, OrderManagementAgent
from ecommerce_agents.api.schemas import AgentRequest, AgentResponse, HealthResponse
from ecommerce_agents.config import get_settings
from ecommerce_agents.db.pool import Database
from ecommerce_agents.observability.tracing import configure_tracing, flush_traces

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Enable tracing, open the DB pool and build the agent; close and flush on shutdown.

    Args:
        app: The FastAPI application; ``settings``, ``db`` and ``agent`` are
            stored on ``app.state`` for the handlers.

    Yields:
        None while the application is serving requests.

    Raises:
        ConfigError: If required secrets are missing (startup fails fast).
    """
    settings = get_settings()
    configure_tracing(settings)
    db = Database(settings)
    await db.open()
    try:
        app.state.settings = settings
        app.state.db = db
        app.state.agent = OrderManagementAgent.create(settings, db)
        logger.info("component=api status=ready agent=%s", AGENT_NAME)
        yield
    finally:
        await db.close()
        flush_traces()


app = FastAPI(
    title="Order Management Agent",
    description="LangGraph order management agent for ecommerce customer support",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().api.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _agent(request: Request) -> OrderManagementAgent:
    """Fetch the agent built at startup.

    Args:
        request: Incoming request (gives access to ``app.state``).

    Returns:
        The shared agent.

    Raises:
        HTTPException: 503 if the agent is not initialised.
    """
    agent: OrderManagementAgent | None = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    return agent


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Report dependency health (DB reachable, LLM key configured).

    Does not call the LLM, to avoid cost and latency on every probe.

    Args:
        request: Incoming request.

    Returns:
        Health flags; ``status`` is ``degraded`` if the database is unreachable.

    Raises:
        HTTPException: 503 if the app has not finished starting.
    """
    _agent(request)
    db_ok = await request.app.state.db.ping()
    settings = request.app.state.settings
    return HealthResponse(
        status="healthy" if db_ok else "degraded",
        database_connection=db_ok,
        llm_configured=settings.secrets.groq_api_key is not None,
        checkpointer=settings.persistence.checkpointer.backend,
    )


@app.post("/process", response_model=AgentResponse)
async def process(body: AgentRequest, request: Request) -> AgentResponse:
    """Answer one customer message.

    Args:
        body: Customer message plus optional customer and session ids.
        request: Incoming request.

    Returns:
        The agent's answer and the session id to reuse for follow-ups.

    Raises:
        HTTPException: 503 if the agent is not initialised.
    """
    result = await _agent(request).run(
        body.customer_message, customer_id=body.customer_id, session_id=body.session_id
    )
    return AgentResponse(
        response=result.response,
        session_id=result.session_id,
        tool_calls=result.tool_calls,
        processing_time=result.processing_time,
    )


@app.post("/process/stream")
async def process_stream(body: AgentRequest, request: Request) -> StreamingResponse:
    """Stream one turn as newline-delimited JSON events.

    Events: ``progress`` (node + tool names), ``token`` (LLM text), ``error``,
    then a final ``complete`` event carrying the ``session_id``.

    Args:
        body: Customer message plus optional customer and session ids.
        request: Incoming request.

    Returns:
        An ``application/x-ndjson`` streaming response.

    Raises:
        HTTPException: 503 if the agent is not initialised.
    """
    agent = _agent(request)
    session_id = body.session_id or str(uuid.uuid4())

    async def events() -> AsyncIterator[str]:
        """Serialize agent events as NDJSON lines.

        Yields:
            One JSON line per event, ending with a ``complete`` event.
        """
        async for event in agent.stream(
            body.customer_message, customer_id=body.customer_id, session_id=session_id
        ):
            yield json.dumps(event) + "\n"
        yield json.dumps({"type": "complete", "session_id": session_id}) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
