# Agent evals

Placeholder for LangGraph agent evaluations (trajectory and response-quality checks
against a dataset, real LLM). They are slow, cost money and are non-deterministic, so
they live apart from `unit/` and `integration/` and are excluded from default runs.

Mark eval tests with `@pytest.mark.eval` and run them explicitly:
`uv run pytest tests/evals -m eval`.
