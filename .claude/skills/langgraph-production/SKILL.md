---
name: langgraph-production
description: Best practices for building production-grade, scalable multi-agent systems with LangGraph (state design, supervisor/handoff/subgraph topologies, persistence and checkpointers, human-in-the-loop interrupts, retries and error handling, execution limits, tool safety, memory, streaming, observability, testing, deployment). Use this whenever designing, adding, or reviewing a LangGraph graph, node, router, agent, subgraph, tool, checkpointer, interrupt, or multi-agent workflow, or when asked how to make an agent reliable, scalable, or production-ready. Covers LangGraph design and runtime decisions; file placement is in project-structure and docstring/logging style is in CLAUDE.md.
---

# LangGraph production best practices

Use this as a checklist while designing or reviewing LangGraph code. It records the
decisions that separate a demo from a system you can run, debug and scale. Follow
the project's `CLAUDE.md` for docstrings and logging, and the `project-structure`
skill for where files go. This skill does not repeat them.

LangGraph's API moves quickly (streaming, durability and interrupt APIs have all
changed between minor versions). Before writing code against an API you are not
certain about, check the installed version and confirm the signature with the
Context7 docs (`/websites/langchain_oss_python_langgraph`) rather than relying on
memory.

Code patterns for the rules below live in `references/patterns.md`. Read it when
you are about to write the corresponding code.

## 1. Start with the simplest topology that works

Complexity is the main production risk. Pick the lowest rung that solves the task,
and move up only with a concrete reason.

| Rung | Use when | Cost of moving up |
|---|---|---|
| Plain LLM call or fixed chain | Steps are known in advance | none |
| Single agent + tools (ReAct loop) | One skill set, dynamic tool choice | more tokens, harder to bound |
| Workflow graph (explicit nodes/edges, conditional routing) | Steps are known but branch on state | more code |
| Supervisor + specialist agents | Distinct domains/tools/prompts that a single agent handles poorly (tool overload, prompt bloat) | routing errors, extra LLM hop, context loss |
| Handoff / swarm (agents transfer control directly) | Conversation should stay with the specialist after a transfer | harder to reason about who is active |
| Nested subgraphs | A specialist has its own multi-step flow, or separate teams own agents | state-mapping code |

Rules of thumb:

- Justify every extra agent. Each one adds latency, cost, a routing failure mode and
  context to pass along. "Different prompt and different tools" is a good reason;
  "it feels more modular" is not.
- Keep the supervisor thin: it routes, it does not do domain work.
- Prefer deterministic routing (code on state) over LLM routing wherever the
  decision can be computed. Use LLM routing only for genuinely fuzzy intent.
- Give each specialist a small, focused tool set (roughly under 10). Tool
  selection accuracy drops as the list grows.

## 2. Design state deliberately

State is the contract between nodes, and the thing that gets checkpointed. Treat it
like a database schema.

- Use `TypedDict` or Pydantic for state. No untyped dicts.
- Store **the minimum**: identifiers, decisions, structured results, and messages.
  Do not put large documents, embeddings, file bytes or full API payloads in state;
  every checkpoint serializes and stores them. Store a reference (ID, URL, key) and
  fetch on demand.
- Every key that more than one node (or parallel branch) writes needs a **reducer**
  (`Annotated[list[X], operator.add]`, `add_messages`, or a custom function).
  Without one, the last writer silently wins or parallel writes raise
  `InvalidUpdateError`.
- Separate schemas: an `InputState` and `OutputState` for the public boundary and a
  richer internal state (`StateGraph(State, input_schema=..., output_schema=...)`),
  so callers cannot see or set internals.
- Use **private state keys** for scratch data that should not leak to the parent or
  the caller.
- Nodes return **partial updates** (only the keys they change), never mutate `state`
  in place, and never return the whole state.
- State that is serialized must stay serializable across deploys. When you rename or
  remove a key or change its type, assume threads checkpointed under the old shape
  still exist (see section 4, schema evolution).
- Keep runtime dependencies (DB pools, clients, user ID, tenant ID, feature flags)
  out of state. Pass them through `context_schema` / `Runtime` or `config`, so they
  are not checkpointed.

## 3. Nodes, edges and routers

- **Single responsibility per node**: one LLM call, one tool batch, or one
  transformation. Fine-grained nodes give you granular checkpoints, targeted retries,
  clean traces and testable units.
- **Idempotent nodes.** Persistence and retries mean a node may run more than once
  (retry, resume after crash, replay after an interrupt). Side effects (send email,
  charge card, create ticket) must be idempotent: use an idempotency key derived from
  thread/run/step, or check-then-act. Never place two unrelated side effects in one
  node.
- **Routers are pure functions of state.** No I/O, no LLM call, no mutation. Annotate
  the return type as `Literal[...]` of the possible next nodes so graph drawing and
  type checkers work, and always include a safe default branch that terminates or
  escalates, never a dead end.
- Use `Command(goto=..., update=...)` when a node must both update state and choose
  the next node (handoffs). Use conditional edges when routing is a separate concern.
- Use `Send` for fan-out (map-reduce) over a dynamic number of items, with a reducer
  on the collecting key. Cap the fan-out width.
- Every graph needs an explicit termination path. Model "give up / escalate to a
  human / return a partial answer" as a first-class node, not an exception.
- Compile once at startup and reuse the compiled graph across requests. Building the
  graph per request is slow and leaks resources.

## 4. Persistence, threads and durability

- **Development**: `InMemorySaver`. **Production**: a durable checkpointer
  (`PostgresSaver` / `AsyncPostgresSaver`, or the platform's managed one). Never ship
  `InMemorySaver`; a restart loses every conversation and every paused approval.
- Run the checkpointer's `setup()` (migrations) as a deploy step, not lazily on first
  request. Use a connection pool sized for your concurrency, and close it on
  shutdown.
- **Thread IDs are your session identity.** Generate them server-side (UUID) or
  derive them from an authenticated conversation ID. Never trust a client-supplied
  thread ID without checking it belongs to that user/tenant, otherwise one user can
  read or resume another's conversation. Keep them within the column limit (255 for
  Postgres).
- Choose a **durability mode** deliberately (sync / async / exit) based on how much
  you can afford to lose versus checkpoint write latency. Use synchronous
  persistence for flows with irreversible side effects or approvals.
- **Short-term vs long-term memory**: the checkpointer holds per-thread state; the
  **store** (`BaseStore`, Postgres-backed in production) holds cross-thread memory
  such as user preferences, namespaced by `(tenant, user, ...)`. Do not use thread
  state as a long-term database.
- **Bound conversation growth.** Unbounded `messages` inflates cost and latency and
  eventually breaks the context window. Trim or summarize (`trim_messages`,
  summarization node) before the model call, and keep the full history in the
  checkpoint or an external log if it must be audited. Use a dedicated summarization
  step, not silent truncation of tool-call/tool-result pairs (orphaned tool results
  cause provider errors).
- **Schema evolution**: adding optional keys with defaults is safe; renaming or
  retyping keys breaks in-flight threads. Version your state (`schema_version` key)
  and write a migration or a compatibility read path before removing anything.
- Clean up: define a retention policy for old threads and checkpoints (TTL or a
  scheduled job). They grow forever otherwise.

## 5. Human-in-the-loop

- Use `interrupt()` inside the node that needs approval or input, and resume with
  `Command(resume=...)` on the same `thread_id`. This requires a checkpointer.
- **Everything before `interrupt()` in that node re-executes on resume.** Put
  side effects after the interrupt (or in a preceding node), and keep the code before
  it idempotent and cheap.
- Do not wrap `interrupt()` in a bare `try/except` that swallows it. The interrupt
  is implemented with an exception.
- Multiple interrupts in one node are matched to resume values by order; keep their
  order stable. For parallel branches that interrupt at the same time, resume with a
  map of interrupt ID to value.
- Interrupt payloads and resume values must be serializable and small. Send what the
  reviewer needs to decide (action, arguments, risk), not the whole state.
- Gate **irreversible or high-value actions** (refunds, cancellations, deletes,
  external messages, payments over a threshold) behind an approval step. Make the
  threshold configuration, not code.
- Treat resume values as untrusted input: validate them against a schema before
  acting.
- A paused thread can wait days. Decide what happens on timeout (auto-reject,
  reminder, expire) outside the graph, and make sure state does not go stale (for
  example, re-check inventory or price after resume).

## 6. Reliability: retries, errors, limits

- Attach a `RetryPolicy` per node with `retry_on` limited to **transient** errors
  (timeouts, 429, 5xx, connection resets). Never retry validation errors, auth
  failures or bugs. Use exponential backoff with jitter and a low max attempts.
- Handle **tool errors inside the tool node** and return them to the model as a
  structured `ToolMessage` (`status="error"`, short actionable text) so the model can
  correct itself. Cap self-correction loops.
- Handle **LLM failures** with model fallbacks (secondary provider/model), request
  timeouts, and a max-retries setting on the client. Do not stack client-level
  retries with node-level retries without accounting for the multiplication.
- **Bound every loop.** Set `recursion_limit` explicitly per invocation (the default
  is a safety net, not a design). Catch `GraphRecursionError` and route to graceful
  degradation. For agent loops also track a step/tool-call counter and a token or
  cost budget in state and exit when exceeded.
- Set wall-clock timeouts at the request level and per tool call. A hung tool must
  not hold a worker forever.
- Add a **circuit breaker or rate limiter** in front of flaky or rate-limited
  dependencies so a downstream outage fails fast instead of piling up retries.
- Decide the failure mode for each node up front: retry, fall back, skip with a
  degraded answer, or escalate to a human. Write it down in the node docstring.
- Never let raw exceptions or stack traces reach end users. Map to a safe message and
  log the detail.

## 7. Tools and agent boundaries

- Each tool has a **narrow purpose, a typed args schema (Pydantic), and a precise
  description**. The description is the prompt the model reads to choose the tool;
  state when to use it and when not to.
- **Authorization lives in the tool, not the prompt.** Enforce tenant/user scoping in
  code using identity from `config`/`context`, never from arguments the model can
  fill in. A model can be talked into passing another customer's ID.
- Validate tool inputs and outputs. Return compact, structured results: trim fields
  the model does not need, cap list sizes, and paginate. Large tool output is the top
  cause of blown context windows.
- Split **read tools from write tools**. Read tools are safe to retry and cache.
  Write tools need idempotency keys, confirmation for destructive ones, and audit
  logging.
- Prefer exposing a handful of coarse business operations over raw CRUD or raw SQL.
- Treat retrieved documents and tool output as **untrusted data** (prompt injection).
  Keep instructions in the system prompt, wrap external content clearly as data, and
  never let tool output alone authorize an action.
- Give each specialist agent **least privilege**: only the tools it needs.

## 8. Multi-agent specifics

- **Define handoff contracts.** When agent A transfers to B, specify exactly what
  goes across: a task description plus the structured facts B needs, not the raw full
  transcript by default. Full-history sharing is simple but expensive and leaks
  irrelevant context; pick it consciously.
- **Message hygiene across agents.** When returning from a subagent, return a
  concise result (final answer or structured summary), not its whole
  tool-calling scratchpad. Ensure every tool call in shared history has its matching
  tool result before the next model call.
- **Subgraph state**: if parent and subgraph share keys, add the compiled subgraph
  directly as a node. If the schemas differ, wrap it in a node function that maps
  parent state into the subgraph input and the output back. Prefer the mapping
  wrapper at team boundaries so subgraphs can evolve independently.
- To jump out of a subgraph to a parent-level node use
  `Command(goto="node", graph=Command.PARENT)`.
- Name agents and nodes uniquely and stably. Names appear in traces, checkpoints and
  handoff tools, and renaming breaks in-flight threads.
- **Agent registry**: register agents in one place (a dict or factory) and derive the
  supervisor's routing options and handoff tools from it, so adding an agent is one
  change.
- Guard against **ping-pong**: track the handoff count or the last agent in state and
  stop or escalate after N transfers.
- Use structured outputs (`with_structured_output` / Pydantic) for supervisor
  decisions, classifications and extraction. Never parse free-text with regex to
  decide control flow.
- Run independent specialists **in parallel** (fan-out with `Send` or multiple edges
  from one node) when their work does not depend on each other, and merge through
  reducers.

## 9. Prompts, models and cost

- Prompts are versioned files, loaded via config (see `project-structure`). Include
  the prompt version in traces.
- Pick the model per node: a small/fast model for routing and classification, a
  stronger one for reasoning and final answers. Make the model name configuration.
- Use **prompt caching**: keep the stable prefix (system prompt, tool definitions)
  first and volatile content (user turn, retrieved chunks) last.
- Cap output tokens per call and budget tokens per run. Track cost per thread.
- Do not let the model see data it does not need (minimize PII in prompts).

## 10. Streaming and API surface

- Stream to the user with the appropriate `stream_mode` (`messages` for tokens,
  `updates` for node progress, `custom` for your own progress events via the stream
  writer). Use async (`ainvoke`/`astream`) end to end in a web service; a blocking
  call in an async handler stalls the whole event loop.
- Expose interrupts and errors as first-class response types, so the client can
  render an approval prompt or a retry, and can resume with the same thread ID.
- Make request handlers thin: authenticate, resolve/validate `thread_id`, build
  `config` + `context`, call the graph, translate the result. Business logic stays in
  the graph.
- Handle client disconnects: a cancelled request should not leave a half-executed
  write. Combined with idempotent nodes and durable checkpoints this is safe to
  resume.

## 11. Observability

Follow the logging rules in `CLAUDE.md`, and add the LangGraph-specific parts:

- Enable **tracing** (LangSmith or OpenTelemetry) in every environment, and attach
  metadata to each run: `thread_id`, user/tenant (hashed), environment, git commit,
  prompt versions, graph version. Tag runs so a single conversation can be found.
- Pass correlation IDs through `config["metadata"]` and `config["tags"]`, and add
  them to every log line.
- Track metrics per node and per agent: latency (p50/p95), error rate, retry count,
  token usage, cost, tool success rate, handoff counts, interrupt-to-resume time, and
  loop-limit hits. Alert on the last two.
- Redact PII before it reaches traces where they leave your boundary.
- Capture user feedback and outcome signals (resolved, escalated, thumbs down) and
  attach them to the trace for later evaluation.

## 12. Testing and evaluation

- **Unit-test nodes and routers as plain functions.** They take state in and return
  updates, so they need no LLM. Test every router branch, including the default.
- **Test graph wiring with a fake chat model** (scripted responses) and an
  `InMemorySaver`: assert the path taken, the final state, and interrupt/resume
  behaviour. These run in CI on every commit.
- Test failure paths on purpose: tool raises, model times out, recursion limit hit,
  bad resume value, empty retrieval.
- **Evaluate, don't just test.** Keep a versioned dataset of real and synthetic
  conversations. Score both the **final outcome** and the **trajectory** (right
  agent chosen, right tools called, no unnecessary steps) with deterministic checks
  first and LLM-as-judge for the fuzzy parts. Run it before every prompt, model or
  graph change and compare against the baseline.
- Real-LLM tests belong in a separate, non-blocking suite (see `project-structure`).
- Run a small set of adversarial cases: prompt injection through tool output,
  cross-tenant ID substitution, requests to exceed authority.

## 13. Security and guardrails

- Input guardrails (length, PII handling, injection screening) before the graph;
  output guardrails (policy, PII leakage, grounding) before the response. Implement
  them as nodes or middleware so they show up in traces.
- Secrets come from a secret manager or environment, never from state, prompts,
  logs or checkpoints. Remember checkpoints persist state verbatim.
- Encrypt checkpoint storage at rest, and encrypt sensitive state fields if the store
  is shared. Apply the same access controls as your primary database.
- Rate-limit per user and tenant at the API layer to stop runaway cost.
- Keep an **audit log** of state-changing tool calls: who (user, agent), what
  (tool, sanitized args), when, and result, separate from debug logs.

## 14. Deployment and scaling

- Keep the API process **stateless**. All conversation state is in the
  checkpointer/store, so any replica can serve any thread and you can scale
  horizontally.
- Serialize concurrent runs on the same `thread_id` (queue or lock) so two requests
  do not fork one conversation. Different threads run fully in parallel.
- Use a task queue or background worker for long-running or scheduled runs, and
  webhooks or polling to deliver results. Do not hold an HTTP request open for a
  multi-minute agent run.
- Size the Postgres pool per replica and watch total connections. The checkpointer
  is often the first bottleneck.
- Roll out graph changes carefully: in-flight and paused threads will resume on the
  new code. Keep changes backward compatible (section 4), or drain or version them.
- Use LangGraph Platform / LangGraph Server if you want managed persistence, queues,
  cron and streaming instead of building them; otherwise wrap the compiled graph in
  your own service (FastAPI is the common choice).
- Configure via environment (dev/staging/prod files), pin dependency versions
  (including `langgraph`, `langchain-core` and the checkpointer package), and run
  the eval suite on upgrades.

## Review checklist

Use this when reviewing or finishing a graph. Every "no" needs a reason.

- [ ] Is each agent justified over a simpler rung, and does the supervisor only route?
- [ ] Is state typed, minimal, with reducers on all multi-writer keys?
- [ ] Are runtime dependencies and identity in `context`/`config`, not state?
- [ ] Are nodes single-purpose and idempotent, with routers pure and defaulted?
- [ ] Is there a durable checkpointer, server-generated or validated thread IDs?
- [ ] Is conversation history bounded (trim or summarize)?
- [ ] Are irreversible actions gated by `interrupt()`, with side effects after it?
- [ ] Do transient failures retry (only those), and does every loop have a limit?
- [ ] Is authorization enforced inside tools, and external content treated as data?
- [ ] Are handoffs explicit, ping-pong guarded, subagent output summarized?
- [ ] Is every run traceable by thread/run ID, and are cost and latency measured?
- [ ] Do unit tests cover routers, and are trajectories evaluated on a dataset?
- [ ] Is the service stateless, with concurrency on a thread serialized?
- [ ] Can in-flight threads survive the next deploy?

## When not to apply this

For a learning spike or a first prototype, do not add everything at once. Start with
sections 1 to 3 and an `InMemorySaver`, then add persistence, limits, observability
and evals as the system approaches real users. Say which parts you are deferring so
they are visible, not forgotten.
