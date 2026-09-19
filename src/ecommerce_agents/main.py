"""Entry point that serves the FastAPI app with uvicorn (``uv run order-agent``).

Configures logging (the only place besides the CLI allowed to touch the root
logger), passes uvicorn the Windows event-loop factory required by psycopg, then starts
uvicorn using host/port from ``config/<env>.yaml``.
"""

import sys

import uvicorn

from ecommerce_agents.config import get_settings
from ecommerce_agents.observability.logging import configure_logging


def main() -> None:
    """Start the API server.

    Raises:
        ConfigError: If the config file for the current environment is missing.
    """
    settings = get_settings()
    configure_logging(settings.app.log_level)

    # uvicorn forces the Proactor loop on Windows, which psycopg rejects; pass our own factory
    loop = "ecommerce_agents.db.pool:selector_loop_factory" if sys.platform == "win32" else "auto"

    # log_config=None keeps our root logging setup instead of uvicorn's own
    uvicorn.run(
        "ecommerce_agents.api.app:app",
        host=settings.app.host,
        port=settings.app.port,
        log_config=None,
        loop=loop,
    )


if __name__ == "__main__":
    main()
