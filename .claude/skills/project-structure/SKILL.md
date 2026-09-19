---
name: project-structure
description: Directory and file placement best practices for production Python projects, especially agentic AI / LLM services (src-layout, package-by-responsibility, agents, tools, prompts, config, db, controls, guardrails, observability, tests, scripts, docs). Use this whenever adding a new module, agent, tool, prompt, config value, DB query, script, or test, whenever asked where a file should live, how to organize or scaffold a repo, or to review a project's directory structure. Covers placement and naming only, not docstring or comment style.
---

# Project structure conventions

Organize by **responsibility, not by layer**. Each directory owns one concern and each
file owns one noun (`tools_web.py` owns web tools, `billing_agent.py` owns the billing
agent). Before creating a file, find the directory whose responsibility matches. New
top-level directories should be rare.

First, look at the existing repo. If it already has a convention, follow it over the
defaults below. Use these defaults for new projects or when the repo has no convention.

## Default layout

```
<repo>/
├── pyproject.toml
├── README.md
├── CLAUDE.md                # project instructions for Claude Code
├── config/                  # per-environment config files (dev.yaml, prod.yaml)
├── scripts/                 # standalone operational scripts, never imported by src/
├── tests/                   # see "Tests" below
├── docs/                    # human-readable docs
└── src/<package>/           # the installable package (src-layout)
    ├── __init__.py
    ├── main.py              # entrypoint, wired via [project.scripts]
    ├── config.py            # the single place config is read
    ├── agents/              # one file per agent + shared agent infra
    ├── tools/               # tool/MCP server code, one file per tool domain
    ├── prompts/             # one .md file per prompt
    ├── db/                  # pool, queries, schema, seeds
    ├── controls/            # execution limits: timeouts, step budgets, rate limits
    ├── guardrails/          # safety and validation checks
    └── observability/       # logging, tracing, metrics
```

Only create the directories a project needs. An agentic project uses most of them, a
plain library uses very few.

## Why each choice

- **`src/` layout**: stops code from accidentally importing from the working
  directory instead of the installed package. Recommended by the Python Packaging
  Authority.
- **Package by responsibility**: `agents/`, `db/`, `tools/` scale better than
  layer folders like `models/`, `utils/`, `helpers/`, which turn into dumping grounds.
- **Prompts as `.md` files**: they can be versioned, diffed and edited apart from
  code. Load them through config; never hardcode prompt strings in Python.
- **One config entry point**: read config only in `config.py`. Feature code asks
  `config.py` for values; it does not read env vars or YAML directly.
- **`scripts/` outside the package**: one-off tooling (smoke tests, migrations,
  diagram generation) should not ship with the app or be imported by it.
- **`__init__.py` in every package**: an explicit marker and a place to grow a public
  API, even when it starts empty.

## Adding new things

- **New agent**: `agents/<name>_agent.py` exposing one entry function (for example
  async `run(...)`). Register it in one central place (a dict in the supervisor or
  router), not scattered `if/elif` branches. Give it a prompt file and any config it
  needs.
- **New tool**: add to the existing `tools/<domain>.py`, or create a new file only
  when the domain is genuinely new. Register it where the other tools are registered.
- **New prompt**: `prompts/<name>.md`, loaded through config.
- **New config value**: add it to the config file and expose it through a getter in
  `config.py`.
- **New DB access**: query functions go in `db/queries.py`, DDL in `db/schema.sql`.
  Use the shared connection pool; do not open ad hoc connections.
- **New execution limit** (timeout, step budget, rate limit): `controls/`.
- **New safety check**: `guardrails/`. **New logging, tracing or metrics code**:
  `observability/`. If these packages exist as empty placeholders, fill them in
  rather than creating a parallel location.
- **New test**: `tests/unit/<mirrored path>/test_<module>.py` (or
  `tests/integration/` if it needs a real service, `tests/evals/` if it needs a real
  LLM). Put new fixtures in the nearest `conftest.py` that covers all their users.
- **New one-off script**: `scripts/`, run directly, never imported from `src/`.
- **New implementation-step write-up**: `docs/execution/stepN-<slug>.md`, continuing
  the existing numbering. Copy the structure of the latest existing step doc.
- **New package**: give it an `__init__.py`.

## Tests

Split by speed and cost first, then mirror `src/<package>/` inside `unit/`:

```
tests/
├── conftest.py            # only fixtures BOTH layers need; omit if there are none
├── unit/                  # fast: no network, no DB, LLM mocked
│   ├── conftest.py        # unit-only fixtures (settings, fake LLM, fake clients)
│   ├── test_config.py     # mirrors src/<package>/config.py
│   ├── agents/test_<name>.py
│   ├── tools/test_<name>.py
│   └── observability/test_<name>.py
├── integration/           # real DB and/or real tool server process
│   ├── conftest.py        # pool/client fixtures; skips when the service is not configured
│   └── test_<name>.py
└── evals/                 # agent evals: trajectory/quality on a dataset, real LLM
    └── test_<name>.py
```

- **Mirror the source.** The test for `src/<package>/tools/order_tools.py` is
  `tests/unit/tools/test_order_tools.py`. Every folder under `tests/` needs an
  `__init__.py` so duplicate basenames in different folders do not collide.
- **Fixtures live at the lowest level that needs them.** Unit-only fixtures go in
  `tests/unit/conftest.py`, DB fixtures in `tests/integration/conftest.py`. The root
  `conftest.py` is only for what both layers share.
- **Folders and markers work together.** Folders decide where a test lives; register
  markers in `pyproject.toml` (`integration`, `slow`, `eval`, with `--strict-markers`)
  and tag tests (`pytestmark = pytest.mark.integration`) so
  `pytest -m "not integration and not eval"` runs everything safe anywhere.
- **Integration tests skip, never fail, without credentials.** Do the skip in the
  fixture (`pytest.skip(...)` when the connection string is unset), not with per-module
  boilerplate. Point them at a dedicated test schema or branch and keep them read-only
  against seeded data; never run them against real customer data.
- Unit tests run in seconds on every commit and in CI. Integration tests need
  services, so run them separately (`pytest tests/unit` vs `pytest tests/integration`).
- **Evals** (`tests/evals/`, marker `eval`) call a real LLM: slow, paid and
  non-deterministic, so they are excluded from normal runs and CI on every push.
- Keep `__pycache__/` and `.pytest_cache/` in `.gitignore`.

## Maturity checklist

When a project passes the prototype stage, recommend these (do not add them
unprompted):

- `tests/` with the layout above
- Lint and type-check config (ruff, mypy) in `pyproject.toml`
- `Dockerfile` and `docker-compose.yml` once there are service dependencies like a DB
- CI workflow that runs unit tests on every push

## When something does not fit

If new code does not match any directory, pause. Do not force it into the nearest
folder or invent a top-level package on the spot. Ask the user which placement they
prefer, because wrong placement is expensive to undo once other code depends on it.

## Out of scope

This skill covers where files go and how directories are named. Docstring and comment
style belongs to the project's `CLAUDE.md`. Follow both.
