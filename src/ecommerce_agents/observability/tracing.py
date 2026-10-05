"""LangSmith tracing setup.

LangChain/LangGraph pick up tracing from process environment variables, but our
config loader reads ``.env`` into typed settings without exporting it. So
:func:`configure_tracing` translates the resolved settings into the
``LANGSMITH_*`` variables, once, at startup (API lifespan and CLI). Every graph
run then shows up in LangSmith with the tags and metadata set in
``OrderManagementAgent._run_config`` (session id, run id, agent, environment).

Traces contain prompts, tool results and completions, so they leave the
process boundary; keep real customer PII out of the dev dataset or redact it
before enabling tracing against production data.
"""

import logging
import os

from langchain_core.tracers.langchain import wait_for_all_tracers

from ecommerce_agents.config import Settings

logger = logging.getLogger(__name__)


def configure_tracing(settings: Settings) -> bool:
    """Enable or disable LangSmith tracing from settings.

    Tracing is on only if ``tracing.enabled`` is true, ``LANGSMITH_API_KEY`` is
    set, and ``LANGSMITH_TRACING`` is not explicitly false. When off, tracing is
    explicitly disabled so an ambient environment variable cannot enable it.

    Args:
        settings: Resolved settings (YAML tracing section plus LangSmith secrets).

    Returns:
        True if tracing was enabled, False otherwise.
    """
    secrets = settings.secrets
    api_key = secrets.langsmith_api_key.get_secret_value() if secrets.langsmith_api_key else ""
    enabled = settings.tracing.enabled and bool(api_key) and secrets.langsmith_tracing is not False

    if not enabled:
        os.environ["LANGSMITH_TRACING"] = "false"
        reason = "disabled_in_config" if not settings.tracing.enabled else "missing_key_or_flag"
        logger.info("component=tracing status=off reason=%s", reason)
        return False

    project = secrets.langsmith_project or settings.tracing.project
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = api_key
    os.environ["LANGSMITH_PROJECT"] = project
    if secrets.langsmith_endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = secrets.langsmith_endpoint

    logger.info(
        "component=tracing status=on project=%s endpoint=%s",
        project,
        secrets.langsmith_endpoint or "default",
    )
    return True


def flush_traces() -> None:
    """Block until queued traces are uploaded to LangSmith.

    Traces upload from a background thread; call this on shutdown so short-lived
    processes (the CLI, a stopping server) do not drop the last runs. Safe to
    call when tracing is off.
    """
    wait_for_all_tracers()
