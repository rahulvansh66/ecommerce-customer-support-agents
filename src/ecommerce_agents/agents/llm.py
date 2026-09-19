"""Chat model factory (Groq) shared by the agents.

Builds a ``ChatGroq`` client from ``config/dev.yaml`` (model, temperature,
timeout, retries) and the API keys in ``.env``. If ``GROQ_FALLBACK_API_KEY`` is
set, a second client with that key is tried when the primary fails with a
transient or auth error (rate limit, 5xx, connection/timeout, bad key). The
fallback is an explicit wrapper rather than ``Runnable.with_fallbacks``, which
produces broken LLM runs in LangSmith traces with the current langchain-core.
"""

import logging
from collections.abc import Sequence
from typing import Any

import groq
from langchain_core.language_models import LanguageModelLike
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_core.tools import BaseTool
from langchain_groq import ChatGroq
from pydantic import SecretStr

from ecommerce_agents.config import Settings

logger = logging.getLogger(__name__)

# Errors worth retrying on the fallback key. Client-side retries (max_retries) have
# already been exhausted by the time these reach us. Bugs and bad requests are not included.
FALLBACK_ERRORS: tuple[type[Exception], ...] = (
    groq.RateLimitError,
    groq.InternalServerError,
    groq.APIConnectionError,  # includes APITimeoutError
    groq.AuthenticationError,
    groq.PermissionDeniedError,
)


def _make_client(settings: Settings, api_key: str) -> ChatGroq:
    """Create one ChatGroq client.

    Args:
        settings: Settings supplying model name, temperature, token cap, timeout and retries.
        api_key: Groq API key for this client.

    Returns:
        A configured ``ChatGroq`` instance.
    """
    cfg = settings.llm
    return ChatGroq(
        model=cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        timeout=cfg.timeout_s,
        max_retries=cfg.max_retries,
        api_key=SecretStr(api_key),
    )


def chain_with_fallbacks(models: Sequence[LanguageModelLike]) -> RunnableLambda:
    """Try each model in order, moving on only for :data:`FALLBACK_ERRORS`.

    The wrapper forwards the caller's ``RunnableConfig``, so callbacks (LangSmith
    tracing, LangGraph token streaming) see each attempt as a normal child run.

    Args:
        models: Chat models in priority order (primary first); at least one.

    Returns:
        A runnable taking a message list and returning the first successful ``AIMessage``.

    Raises:
        Exception: Whatever the last model raised if all fail with fallback-worthy
            errors; other exceptions propagate immediately.
    """

    async def call(messages: Any, config: RunnableConfig) -> Any:
        """Invoke the models in order.

        Args:
            messages: Prompt messages for the chat model.
            config: Run config forwarded to the underlying model calls.

        Returns:
            The first successful model response.
        """
        for attempt, model in enumerate(models, start=1):
            try:
                return await model.ainvoke(messages, config)
            except FALLBACK_ERRORS as exc:
                logger.warning(
                    "component=llm status=attempt_failed attempt=%d of=%d error=%s",
                    attempt,
                    len(models),
                    type(exc).__name__,
                )
                if attempt == len(models):
                    raise

    return RunnableLambda(call, name="chat_model_with_fallback")


def build_chat_model_with_tools(
    settings: Settings, tools: Sequence[BaseTool]
) -> LanguageModelLike:
    """Build the tool-bound chat model, with an optional fallback key.

    Args:
        settings: Resolved settings (model config and Groq secrets).
        tools: Tools the model may call; bound to both primary and fallback clients.

    Returns:
        A runnable chat model that accepts a message list and returns an ``AIMessage``
        (a fallback chain if a fallback key is configured, else the primary client).

    Raises:
        ConfigError: If ``GROQ_API_KEY`` is not configured.
    """
    primary = _make_client(settings, settings.require_groq_api_key()).bind_tools(list(tools))

    fallback_key = settings.secrets.groq_fallback_api_key
    if fallback_key and fallback_key.get_secret_value():
        fallback = _make_client(settings, fallback_key.get_secret_value()).bind_tools(list(tools))
        logger.info("component=llm status=ready model=%s fallback=true", settings.llm.model)
        return chain_with_fallbacks([primary, fallback])

    logger.info("component=llm status=ready model=%s fallback=false", settings.llm.model)
    return primary
