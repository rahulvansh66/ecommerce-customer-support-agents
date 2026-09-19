"""Request/response models for the order management API."""

from pydantic import BaseModel, Field

from ecommerce_agents.agents.order_management_agent import AGENT_NAME


class AgentRequest(BaseModel):
    """Body of ``POST /process`` and ``POST /process/stream``."""

    customer_message: str = Field(min_length=1, max_length=2000, description="Customer's message.")
    customer_id: str | None = Field(
        default=None, max_length=50, description="Customer id, e.g. cust001. Scopes order lookups."
    )
    session_id: str | None = Field(
        default=None,
        max_length=100,
        description="Session id from a previous response; omit to start a new conversation.",
    )


class AgentResponse(BaseModel):
    """Body returned by ``POST /process``."""

    response: str
    session_id: str
    agent_type: str = AGENT_NAME
    tool_calls: list[str] = Field(default_factory=list)
    processing_time: float


class HealthResponse(BaseModel):
    """Body returned by ``GET /health``."""

    status: str = Field(description="'healthy' or 'degraded'.")
    database_connection: bool
    llm_configured: bool
    checkpointer: str = Field(description="Checkpointer backend in use.")
