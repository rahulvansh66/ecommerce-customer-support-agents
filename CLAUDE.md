In this project, we are building ecommerce customer support agents from using langgraph.

## Code style

Write code that is structured, modular, scalable, and maintainable. Since this
project is for learning LangGraph, every piece of code must be explained: the
docstrings carry the "what and why" of each unit, and short comments separate
distinct logical blocks within a function (do not restate what a line does).

### Docstrings (follow always)

Every module, class, and function/method must have a docstring. No exceptions for
"small" or "obvious" helpers.

1. **Style.** Use Google-style docstrings consistently across the repo.
2. **Content.** A one-line summary of what it does (and *why*, when not obvious).
   For anything non-trivial, add a short description paragraph.
3. **Every argument is documented.** List all parameters under `Args:` with name,
   type, and meaning. Do not leave a docstring that skips or only partially covers
   the arguments; if you touch a function with a partial docstring, complete it.
4. **Also document** `Returns:` (type + meaning), `Raises:` (exceptions the caller
   should expect), and `Yields:` for generators. Omit a section only when it truly
   does not apply (e.g. no return value).
5. **Keep it in sync.** When you change a signature, update the docstring in the
   same edit.
6. **Module docstring** states the file's purpose and its role in the system
   (e.g. which agent, graph, tool, or pipeline stage it belongs to).
7. **Agent components.** For LangGraph nodes, edges/routers, tools, and
   retrievers, the docstring must state the state keys it reads and writes (or
   the inputs/outputs of the tool), and, for routers, the possible next nodes.

### Typing and logging

- Add type hints to every function/method signature (params and return type).
  Use `typing`/`|` unions consistently; avoid bare `Any` unless the type is
  genuinely dynamic. Use `TypedDict` or Pydantic models for graph state, tool
  inputs/outputs, and structured LLM outputs instead of loose dicts.
- Use the `logging` module, not `print`, for anything that runs as part of the
  application (agent runs, ingestion, retrieval, evaluation). Reserve `print` for
  local, throwaway scripts only.

### Logging conventions (follow always)

Application code (agents, tools, ingestion, API services) may run in containers
or serverless environments where logs are shipped to a central platform such as
CloudWatch. Write logs so they are usable once they land there.

1. **One logger per module**, never configure the root logger from library code:
   `logger = logging.getLogger(__name__)`. Only the entry point (app startup,
   script `main`, worker handler) calls `logging.basicConfig(...)` or sets up
   handlers.
2. **Log levels matter.** `DEBUG` for verbose diagnostics, `INFO` for normal
   progress (node start/end, tool calls, record counts), `WARNING` for
   recoverable issues (retries, fallbacks, empty retrieval results), `ERROR` for
   failures. Use `logger.exception(...)` inside `except` blocks so the traceback
   is captured. Never use `ERROR` for expected control flow.
3. **Structure messages for querying.** Use consistent key=value fields, e.g.
   `logger.info("node=retrieve status=complete docs=%d duration_s=%.2f", n, dt)`
   instead of free-form prose. Use `%s`-style lazy formatting, not f-strings, in
   log calls.
4. **Include correlation identifiers** (thread/session ID, run ID, trace ID,
   agent or node name, git commit hash) so a single conversation or run can be
   traced across agents and tool calls.
5. **Log GenAI-specific signals as aggregates**: model name, prompt/response
   token counts, latency, retry count, tool name, tool success/failure, number of
   retrieved chunks and top similarity score. Do not log full prompts,
   completions, or retrieved documents at `INFO`; if needed for debugging, log
   them at `DEBUG` only, truncated and with sensitive data masked.
6. **Never log secrets or PII.** No API keys, tokens, database credentials, full
   card numbers, or customer PII (names, emails, addresses, order details tied to
   a person) in log messages; mask or omit them. This includes user messages and
   tool arguments that may contain such data.
7. **Respect log volume and retention.** Don't log full DataFrames, embeddings,
   large arrays, or per-item output in tight loops; log counts and samples.
   Set explicit retention on log groups via IaC (short for dev, longer for
   prod/audit) rather than the default "never expire".

### Jupyter notebooks

- Structure the notebook into clear sections and subsections using markdown
  headings (`#`, `##`, `###`), so it reads top-to-bottom as a document, not a
  scratchpad.
- Every section/subsection should be well documented: a short markdown
  description of what it covers and why it's there.
- Precede every code cell with a markdown cell explaining the purpose of that
  specific cell: what it does and why it's needed.
- Code inside cells follows the same rules as `.py` files above (docstrings for
  any function/class defined in the notebook, type hints, `logging` over
  `print`, sparse in-code comments for block separation).
