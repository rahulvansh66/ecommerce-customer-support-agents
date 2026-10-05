"""Unit tests for the Groq key fallback wrapper (fake models; no network)."""

from typing import Any

import groq
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda

from ecommerce_agents.agents.llm import chain_with_fallbacks


def failing(exc: Exception, calls: list[str], name: str) -> RunnableLambda:
    """A fake model that records its call and raises ``exc``."""

    async def run(_m: Any) -> AIMessage:
        calls.append(name)
        raise exc

    return RunnableLambda(run)


def succeeding(text: str, calls: list[str], name: str) -> RunnableLambda:
    """A fake model that records its call and returns ``text``."""

    async def run(_m: Any) -> AIMessage:
        calls.append(name)
        return AIMessage(content=text)

    return RunnableLambda(run)


def connection_error() -> groq.APIConnectionError:
    """A transient error the wrapper should fall back on."""
    return groq.APIConnectionError(request=httpx.Request("POST", "http://test"))


async def test_falls_back_on_transient_error() -> None:
    """A connection error on the primary moves on to the fallback."""
    calls: list[str] = []
    model = chain_with_fallbacks(
        [failing(connection_error(), calls, "a"), succeeding("ok", calls, "b")]
    )
    result = await model.ainvoke([HumanMessage(content="hi")])
    assert result.content == "ok" and calls == ["a", "b"]


async def test_primary_success_skips_fallback() -> None:
    """The fallback is never called when the primary succeeds."""
    calls: list[str] = []
    model = chain_with_fallbacks([succeeding("one", calls, "a"), succeeding("two", calls, "b")])
    assert (await model.ainvoke([HumanMessage(content="hi")])).content == "one"
    assert calls == ["a"]


async def test_non_transient_error_does_not_fall_back() -> None:
    """Bugs propagate immediately instead of being retried on the other key."""
    calls: list[str] = []
    model = chain_with_fallbacks(
        [failing(ValueError("bug"), calls, "a"), succeeding("ok", calls, "b")]
    )
    with pytest.raises(ValueError):
        await model.ainvoke([HumanMessage(content="hi")])
    assert calls == ["a"]


async def test_all_fail_raises_last_error() -> None:
    """If every model fails with a transient error, the last error is raised."""
    calls: list[str] = []
    model = chain_with_fallbacks(
        [failing(connection_error(), calls, "a"), failing(connection_error(), calls, "b")]
    )
    with pytest.raises(groq.APIConnectionError):
        await model.ainvoke([HumanMessage(content="hi")])
    assert calls == ["a", "b"]
