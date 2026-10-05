# ecommerce-customer-support-agents

LangGraph-based ecommerce customer support agents. Currently implemented: the **order management agent**
(order status, order history, inventory, shipping, returns/exchanges, order summary) backed by Neon Postgres
and a Groq LLM, with an in-memory LangGraph checkpointer and store.

## Setup

```bash
uv sync                      # install dependencies into .venv
```

Secrets go in `.env` (never committed): `NEON_POSTGRES_CONNECTION_STRING`, `GROQ_API_KEY`,
optional `GROQ_FALLBACK_API_KEY`. Non-secret settings live in `config/dev.yaml` (`APP_ENV` selects
`config/<env>.yaml`). Seed the demo data once with
`uv run python scripts/neon-db-creation/init_neon.py`.

## Tracing (LangSmith)

Set `LANGSMITH_API_KEY` (and optionally `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`, `LANGSMITH_TRACING`) in
`.env`. `tracing.enabled` in `config/dev.yaml` is the master switch; `tracing.project` is the default
project name. Tracing is enabled at startup by `observability/tracing.py`, and each turn appears as an
`order_management` run tagged with the environment and carrying `session_id`, `run_id` and model metadata.
Traces include prompts and tool results, so avoid real customer PII in traced environments.

## Run

```bash
uv run order-agent-cli --customer-id cust001     # interactive chat
uv run order-agent                               # FastAPI on http://127.0.0.1:8001 (uvicorn)
```

On Windows use `uv run order-agent` rather than calling `uvicorn` directly, or add
`--loop ecommerce_agents.db.pool:selector_loop_factory` (psycopg async needs a selector event loop).

```bash
curl -X POST localhost:8001/process -H 'content-type: application/json' \
  -d '{"customer_message": "Where is order ORD-1002?", "customer_id": "cust001"}'
```

Reuse the returned `session_id` in later requests to continue the conversation. `POST /process/stream`
streams NDJSON events; `GET /health` reports dependency status.

## Test

```bash
uv run pytest tests/unit          # fast, no network
uv run pytest tests/integration   # real Neon queries (skips without a DB configured)
uv run pytest -m "not integration and not eval"   # everything safe to run anywhere
uv run ruff check src tests
```
