"""Interactive command-line chat with the order management agent.

Run with ``uv run order-agent-cli --customer-id cust001``. Useful for trying the
agent without the HTTP server. Chat output uses ``print`` because it is the
user-facing interface; diagnostics still go through ``logging`` (WARNING by
default, raise with ``--log-level INFO``).
"""

import argparse
import asyncio
import logging
import sys
import uuid

from ecommerce_agents.agents.order_management_agent import OrderManagementAgent
from ecommerce_agents.config import get_settings
from ecommerce_agents.db.pool import Database, ensure_selector_event_loop
from ecommerce_agents.observability.logging import configure_logging
from ecommerce_agents.observability.tracing import configure_tracing, flush_traces

logger = logging.getLogger(__name__)

EXIT_WORDS = {"exit", "quit", "q"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Namespace with ``customer_id``, ``session_id`` and ``log_level``.
    """
    parser = argparse.ArgumentParser(description="Chat with the order management agent.")
    parser.add_argument("--customer-id", default=None, help="Customer id, e.g. cust001.")
    parser.add_argument("--session-id", default=None, help="Resume an existing session id.")
    parser.add_argument("--log-level", default="WARNING", help="Log level (default WARNING).")
    return parser.parse_args(argv)


async def chat(customer_id: str | None, session_id: str) -> None:
    """Run the read-eval-print loop until the user exits.

    Args:
        customer_id: Customer the session is scoped to (None = anonymous).
        session_id: Session/thread id, reused across turns so context persists.
    """
    settings = get_settings()
    configure_tracing(settings)
    db = Database(settings)
    await db.open()
    try:
        agent = OrderManagementAgent.create(settings, db)
        print(f"session_id={session_id} customer_id={customer_id or 'none'} (type 'exit' to quit)")
        while True:
            try:
                message = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not message:
                continue
            if message.lower() in EXIT_WORDS:
                break
            result = await agent.run(message, customer_id=customer_id, session_id=session_id)
            print(f"agent> {result.response}\n")
    finally:
        await db.close()
        # traces upload in the background; make sure the last ones are sent before exit
        await asyncio.to_thread(flush_traces)


def main() -> None:
    """CLI entry point: parse args, configure logging, run the chat loop."""
    args = parse_args()
    # Windows consoles default to cp1252, which cannot print some characters models emit
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    configure_logging(args.log_level)
    ensure_selector_event_loop()
    asyncio.run(chat(args.customer_id, args.session_id or str(uuid.uuid4())))


if __name__ == "__main__":
    main()
