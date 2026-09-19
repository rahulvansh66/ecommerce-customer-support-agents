"""Factories for the LangGraph checkpointer and store.

Both are in-memory for now (dev): conversations live only as long as the
process. The backend is selected from ``persistence.*.backend`` in the YAML so
moving to Postgres later means adding a branch here, not touching the agents.
"""

import logging

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from ecommerce_agents.config import Settings

logger = logging.getLogger(__name__)


def build_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    """Create the checkpointer that stores per-thread graph state.

    Args:
        settings: Settings; ``persistence.checkpointer.backend`` picks the backend.

    Returns:
        A checkpointer (``InMemorySaver`` for the ``memory`` backend).
    """
    backend = settings.persistence.checkpointer.backend
    logger.info("component=checkpointer status=ready backend=%s", backend)
    return InMemorySaver()


def build_store(settings: Settings) -> BaseStore:
    """Create the store for cross-thread (long-term) memory.

    Args:
        settings: Settings; ``persistence.store.backend`` picks the backend.

    Returns:
        A store (``InMemoryStore`` for the ``memory`` backend).
    """
    backend = settings.persistence.store.backend
    logger.info("component=store status=ready backend=%s", backend)
    return InMemoryStore()
