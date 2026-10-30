# Automated verification

Phase 10 consolidates the unit, integration, and end-to-end suites required by
[PRD section 29](PRD.md#29-testing). Install development dependencies first:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts/test_suite.py
```

The runner checks Ruff lint, formatting, and pytest, stopping on the first failing
command. Its default mode removes inherited `TEST_DATABASE_URL` and `TEST_REDIS_URL`
from child processes. It needs no database server, GitHub account, or model credits.
On Unix, replace the interpreter path with `.venv/bin/python`.

## Layers and requirement coverage

Markers are assigned by directory and validated with `--strict-markers`.

| Layer | What runs | Representative tests |
| --- | --- | --- |
| `unit` | Signatures, payload/schema validation, risk/recommendation/deduplication, prompts/evidence, provider protocols, graph transitions, Redis status and recovery | `test_signatures.py`, `test_github_schemas.py`, `test_multi_aggregation.py`, `test_review_schemas.py`, `test_llm_service.py`, `test_worker_queue.py`, `test_multi_agent_workflow.py` |
| `integration` | Real migrations and persisted reads, transactional idempotency, vector retrieval, checkpoints, actual queue Lua, publication lifecycle | `test_persistence.py`, `test_review_api.py`, `test_repository_context.py`, `test_context_checkpoints.py`, `test_worker_pipeline.py`, `test_publication.py` |
| `e2e` | Signed webhook → Redis → worker → GitHub/model HTTP adapters → four actual agents → aggregation → database → comment and read APIs | `tests/e2e/test_review_delivery.py` |
| `services` | Cases using explicitly configured PostgreSQL/PGVector or Redis | PostgreSQL fixture variants, real Redis queue test, Redis end-to-end variants |

Run just one layer, including the same static checks:

```powershell
.\.venv\Scripts\python.exe scripts/test_suite.py --layer unit
.\.venv\Scripts\python.exe scripts/test_suite.py --layer integration
.\.venv\Scripts\python.exe scripts/test_suite.py --layer e2e
```

For individual cases, use pytest directly, for example:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e -k lost_response -q
```

Direct pytest honors explicit `TEST_*` environment variables. The runner's default
mode deliberately clears them; use `--services` to retain both.

## HTTP and configuration isolation

Pytest clears application settings inherited from the shell, ignores implicit
`.env` loading, clears cached settings, and disables LangSmith tracing before test
collection and again for each test. Configuration tests can explicitly supply
their own environment values or temporary dotenv file.

Native synchronous and asynchronous HTTPX transports raise immediately during
tests. Tests must inject `httpx.MockTransport` or an ASGI transport. GitHub and model
responses are fixture data, including when the compatible model adapter runs with
`llm_mock_mode=False`. No real provider is contacted. Tests verify that this HTTP
guard rejects accidental requests. This guard covers HTTPX in the pytest process;
it is not a system firewall or a guard for arbitrary child processes.

The end-to-end model fixture returns deterministic JSON through the production
Chat Completions adapter. The actual reviewer prompts, schema checks, repair,
evidence validation, fan-out/fan-in, and scoring run normally. Scenarios verify:

- Full four-agent findings and the expected weighted score of 7.7.
- Complete clean approval, repaired malformed output, partial reviewer failure,
  missing-patch coverage, and all reviewers failing without a successful comment.
- Stale revisions rejected before analysis; invalid signatures and redelivery
  produce no extra job or review.
- Rate-limited publication retries without repeating model work.
- Lost POST response reconciled by authenticated author and frozen body without
  another POST.
- Crash after comment creation but before Redis acknowledgement: a rebuilt worker
  recovers the expired receipt and reuses persisted analysis/publication without
  another model call, review row, finding, or comment.
- Limited file budgets preserve coverage warnings through persistence, API reads,
  and the final partial comment.

Phase 11 also verifies compacted diff line citations, file priority, skipped
oversized lines, no clean approval with limited input, concurrent log correlation,
provider retry correlation, generated demo artifacts, and load-harness
acceptance/deduplication/draining/readback without network access.

## PostgreSQL/PGVector and Redis

Start development instances yourself, then opt in explicitly:

```powershell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://review_agent:review_agent@localhost:5432/review_agent"
$env:TEST_REDIS_URL = "redis://localhost:6379/15"
.\.venv\Scripts\python.exe scripts/test_suite.py --services
# Or the full delivery scenarios only:
.\.venv\Scripts\python.exe scripts/test_suite.py --services --layer e2e
```

`--services` rejects missing URLs before running checks, avoiding an accidentally
service-free result. Supplying a URL whose server is unavailable fails the tests.
Use development resources. PostgreSQL needs the vector extension binaries and a
role permitted to create/drop schemas. Migration 0002 creates the `vector`
extension in `public` if absent, requiring extension-creation privileges; an
administrator can provision it beforehand.

The shared database fixture migrates a unique `test_<uuid>` schema per case and
drops only that schema. PostgreSQL end-to-end cases also use a unique workflow
schema with the production checkpointer and advisory locks. SQLite uses a fresh
file and an in-memory checkpointer; its lock substitute does not verify ownership.
Dedicated integration tests exercise durable checkpoint recreation, interrupted
parallel reviewers, revision-scoped PGVector retrieval, and concurrent writes.

End-to-end cases form the database × queue matrix: SQLite and PostgreSQL, each
with simulated and real Redis when enabled. Production Lua scripts run in both
queue backends. API queue factories select a unique test namespace; cleanup scans
and deletes only keys in that namespace. No test flushes a Redis database. The
delivery scenarios use diff-only context; indexed context is verified by the
repository-context and context-checkpoint integration tests.

## Recorded verification

Verified on Python 3.11.9:

- Default runner: **483 passed, 12 skipped**.
- Service runner with PostgreSQL 16.15 / PGVector 0.8.6 and Redis 7.4.11:
  **608 passed, 11 skipped**.
- End-to-end scenarios: 11 without services; 44 across the service matrix.
- Ruff lint/format checks and installed-dependency compatibility check passed.

Service-enabled skips are SQLite variants of PostgreSQL-only operations. Default
mode also skips the dedicated real Redis test. These are capability skips, not
failed scenarios. No coverage percentage, real-provider accuracy, production
performance, Docker deployment, or CI-platform execution is claimed by these runs.
The subsequent Docker milestone separately verified the packaged stack and added
six fixture/isolation tests: the current default suite is **489 passed, 12 skipped**.
See [DEPLOYMENT.md](DEPLOYMENT.md) for the actual container result and commands.
Recorded in-process simulated-load measurements are documented separately in
[DEMO.md](DEMO.md) and [the benchmark record](../examples/benchmarks/README.md).
