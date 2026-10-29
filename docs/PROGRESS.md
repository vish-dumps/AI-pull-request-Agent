# Implementation progress

The original requirements are in [PRD.md](PRD.md). Continue phase by phase, as
requested there; do not implement the remaining application in one pass.

## Phase 1: foundation

Implemented:

- FastAPI application factory, lifespan-owned resources, Swagger and OpenAPI.
- Pydantic environment configuration and `.env.example`.
- Async SQLAlchemy/asyncpg engine and Redis client with shutdown cleanup.
- Concurrent `/health` checks for PostgreSQL, enabled PGVector, and Redis.
- Bounded checks, HTTP 503 on dependency failure, sanitized responses.
- JSON application logs, non-root Dockerfile, and three-service Docker Compose.
- PostgreSQL initialization script enabling the vector extension.
- README architecture, local setup, verification steps, design decisions, and roadmap.

Verified locally on Python 3.11.9:

- `python -m pytest`: 17 passed. One upstream Starlette HTTPX deprecation warning.
- `python -m ruff check .`: passed.
- `python -m ruff format --check .`: passed.
- `uv pip check`: all installed dependencies compatible.
- Compose YAML parsed and initialization file references checked.
- Real Uvicorn HTTP smoke check: Swagger/OpenAPI returned 200; unavailable
  database/Redis connections produced the expected 503 health response.

Still to verify: Docker is unavailable on this machine. On a Docker-capable host,
run `docker compose up --build -d`, check all containers, and verify `/health`
returns 200 with PostgreSQL, PGVector, and Redis `up`. Follow the README's
stop/start Redis procedure to verify recovery against real infrastructure.

## Phase 2: GitHub integration

Implemented:

- `POST /webhooks/github`: bounded streamed body; raw-byte HMAC-SHA256 validation;
  Pydantic PR payload validation; supported opened/synchronize/reopened events.
- Quick HTTP 202 acceptance through an atomic Redis enqueue script. Delivery UUID
  deduplication prevents a second job for the same delivery during the TTL window.
- Full jobs in `reviews:pending`; expiring `review:{id}:job`, `review:{id}:status`,
  and `github:delivery:{id}` metadata. Pending entries have no TTL.
- Redis AOF and a named volume in Compose. This is basic persistence, not an
  exactly-once delivery guarantee; the default fsync window can lose recent writes.
- Async `GitHubService` with explicit API headers, optional token, bounded GET
  retries, rate-limit handling, pagination, text file access, repository metadata,
  and a comment creation method. No real comments were posted.
- Normalized `PullRequestContext`, including fork/base/head metadata, changed
  files, missing-patch/incomplete-coverage warnings, and SHA consistency checks.
- Signed sample sender and a separate read-only PR fetch command, both included
  in the Docker image. README documents environment variables and verification.

Verification on Python 3.11.9:

- `python -m pytest -q`: 105 passed, with the existing Starlette HTTPX deprecation warning.
- Ruff lint and formatting checks pass.
- GitHub is mocked with `httpx.MockTransport`; the actual Redis Lua script executes
  under `fakeredis[lua]`, including concurrent redelivery and wrong-type failures.
- Integration checks cover signed delivery to queue and queued job to normalized
  context. No paid API calls or real GitHub writes are required.
- Live HTTP smoke check: the actual sender script reached Uvicorn and a TCP fake
  Redis server. The real enqueue script returned 202; redelivery returned the
  same ID with one queue entry; an invalid signature returned 403. Processes were
  stopped after verification. GitHub requests were mocked throughout.
- Both CLI help commands, dependency compatibility, Compose YAML, Redis volume/AOF
  configuration, and Docker demo file references were checked.

There is intentionally no queue consumer yet. The GitHub fetch command reads a PR
independently; accepted webhook jobs stay queued until Phase 8. Do not describe
this as an implemented end-to-end review pipeline. Docker and real GitHub webhook
delivery still need verification on an appropriately configured host.

## Phase 3: relational persistence

Implemented:

- SQLAlchemy models for repositories, PRs, reviews, and findings; UUIDs, relational
  foreign keys, cascades, unique identifiers, indexes, enum checks, and validation
  of result completeness, confidence, scores, and state/timestamp consistency.
- Lowercase repository identity and one PR per `(repository_id, pr_number)`.
  Reviews retain base/head SHAs as PR metadata evolves; IDs reuse queue job IDs.
- Async session factory and request-scoped read sessions. `ReviewRepository`
  provides idempotent creation, eager reads, paginated history, processing,
  completion, and failure operations. Callers own transactions; savepoints handle
  concurrent entity creation and PostgreSQL row locks guard lifecycle changes.
- Findings and final status persist atomically. Terminal reviews cannot be
  restarted/overwritten through repository methods. Internal failure text stays
  out of public read responses.
- Alembic async environment, frozen initial migration `0001`, upgrade/downgrade,
  and Docker startup migration command. No `create_all()` application startup.
- Read APIs: `/reviews/{id}`, `/reviews/{id}/findings`, and
  `/repositories/{owner}/{repo}/reviews?limit=20&offset=0`.
- Idempotent `scripts/seed_demo.py`, clearly labeled synthetic; no GitHub, queue,
  or LLM activity. Fixed review ID: `028594a3-7329-50a1-84b6-98c97dbefcc4`.
- README database diagram, persistence contracts, migration and demo commands.

Verified on Python 3.11.9:

- Default server-free suite: 144 passed, 1 skipped.
- With PostgreSQL 18: 174 passed, 1 skipped. The SQLite concurrency case is
  skipped; its PostgreSQL counterpart passed.
- Both backends apply the actual Alembic migration. Tests cover schema drift,
  downgrade/upgrade, persisted reads, caller rollback, conflicts/idempotency,
  immutable revision snapshots, DB checks, cascades, and API errors/pagination.
- `alembic upgrade head` and `alembic check` passed against PostgreSQL.
- Running the seed command twice produced one review and one finding.
- Real Uvicorn HTTP reads of review, findings, and history returned 200 using
  PostgreSQL, not a database mock. Existing Starlette HTTPX warning remains.
- Ruff lint/format and installed dependency compatibility checks passed.

PostgreSQL verification used a new temporary cluster under `.venv` on a separate
loopback port, using installed PostgreSQL 18 binaries. The existing Windows
PostgreSQL service was not used. Docker Compose startup remains unverified because
Docker is unavailable. The temporary cluster is stopped after verification.

Normal tests use SQLite; set `TEST_DATABASE_URL` explicitly for the PostgreSQL
parameterization. Tests use unique temporary schemas and remove only those schemas.
SQLite remains a test backend, not a supported application database URL.

Webhook acceptance still only writes to Redis. It does not create DB records or
consume work. Phase 8 will connect queued jobs to ingestion/persistence and keep
Redis/database status synchronized. PGVector chunks remain Phase 7 work.

## Phase 4: Security Agent

Implemented:

- A separate security prompt covering supported vulnerabilities, evidence,
  confidence/severity, zero findings, untrusted repository text, and scope exclusions.
- `SecurityFinding`/`SecurityReview` Pydantic schemas with strict object properties,
  required nullable line citations, bounded confidence/risk, evidence snippets, and
  typed success/failure results. Existing DB finding fields remain compatible.
- A small `LLMProvider` protocol with an HTTPX Chat Completions-compatible adapter
  and deterministic mock implementation. Base URL, model, token, output mode,
  token-limit field, timeout, and retry budget come from environment configuration.
- Strict schema response format, plus explicit JSON-object compatibility mode that
  includes the schema in the prompt. No hardcoded model or implicit fallback.
- Bounded transport retries, request deadlines, short Retry-After handling, sanitized
  failures, refusals/incomplete response handling, and one agent output-repair attempt.
- File, patch, metadata, warning, and path input limits. Findings must cite a
  supplied file, exact patch evidence, and an actual added line or null. Output
  repair uses error types rather than echoing raw responses or unknown field names.
- `SecurityReviewService`: ingestion commits before model I/O; a separate transaction
  stores findings and deterministic confidence-weighted risk, or marks a failed run.
  Every security-only success persists as PARTIAL. Zero findings does not approve
  the full PR; the other three reviewer categories are explicitly missing.
- `examples/security_pr.json` and `scripts/review_security.py`. The default/`--mock`
  command runs without infrastructure or LLM credits; `--persist` uses PostgreSQL.
  `--review-id` supports terminal-result reuse without another model call.
- Active LLM configuration in `.env.example` and Compose. No database migration
  was needed. At the end of Phase 4, LangGraph and queue consumption were still
  unimplemented; Phase 5 below adds graph orchestration.

Verification on Python 3.11.9:

- Default suite: 209 passed, 1 skipped.
- With PostgreSQL 18: 243 passed, 1 skipped; the SQLite-only concurrency skip has
  a passing PostgreSQL counterpart. Existing Starlette HTTPX warning remains.
- Provider tests use `httpx.MockTransport`, including schema/JSON-object wire
  formats, headers, bounded retries, deadlines, refusals, and invalid envelopes.
- Agent tests cover one repair, repeated invalid output, unknown files, fabricated
  evidence, false line anchors, category violations, no-network mock mode, input
  budgets, malicious PR instructions as data, and bounded scoring.
- Model-adapter → repair → agent → database integration passed on both backends.
  Tests verify committed ingestion before model I/O and persisted failed results.
- Real CLI mock review persisted in an isolated PostgreSQL cluster; a real Uvicorn
  HTTP read returned PARTIAL, risk score 4.5, one finding, and a [MOCK] summary.
  Repeating the same review ID reused the stored result. Standalone mock mode also
  ran with database/Redis deliberately unavailable.
- Alembic upgrade/check still report revision 0001 with no schema drift.
- Ruff lint/format and installed dependency compatibility checks pass.

No paid LLM calls or GitHub writes were made. Real provider account/model behavior
and Docker Compose startup remain unverified. Mock responses are fixture outputs,
not general vulnerability detection. Citation checks do not prove semantic correctness
or guarantee prompt-injection resistance. The temporary PostgreSQL cluster is
stopped after verification; the existing Windows PostgreSQL service is not used.

This command is manual and intended for one caller per review ID. It does not
consume Redis jobs or synchronize Redis status. Hard-crash recovery for PROCESSING
records, concurrent worker leases, and rescheduling belong to later workflow/worker
work. LangGraph orchestration, other agents, and PGVector retrieval have not run.

## Phase 5: checkpointed LangGraph workflow

Implemented:

- `app/graph/state.py`: JSON-only typed shared state, additive error/stage reducers,
  workflow schema version, and a safe result report. Runtime clients/settings and
  model credentials never enter checkpoint channels.
- `app/graph/nodes.py`: ingest, prepare diff context, Security Agent, aggregate,
  and persist nodes. Ingestion accepts normalized context or fetches it through an
  injected GitHub service. It verifies queued SHAs before persistence.
- Context preparation bounds patches and records `context_source=diff_only`, with
  empty retrieved context. PGVector retrieval remains Phase 7.
- `app/graph/workflow.py`: sequential edges, terminal-result reuse, synchronous
  checkpoint durability, bounded DB node retries, start/resume/report, and guards
  against incompatible input/mode/version/thread reuse.
- Recorded expected/unexpected agent failures finalize as FAILED with safe errors.
  Cancellation and failed infrastructure nodes retain resumable progress.
- `app/graph/checkpoints.py`: official async PostgreSQL saver, strict serializer
  allowlists, autocommit/dictionary rows, and a dedicated `review_workflow` schema.
  Library-owned setup/migrations stay separate from Alembic business revision 0001.
- In-memory checkpoints support infrastructure-free demos/tests. PostgreSQL
  checkpoints support resuming in another process. The CLI selects a Windows
  selector loop for Psycopg 3; business persistence remains SQLAlchemy/asyncpg.
- `services/aggregation_service.py`: shared deterministic risk and lightweight
  same-file/title/nearby-line deduplication. It still produces security-only PARTIAL
  reviews. Other specialized agents and parallel execution are Phase 6.
- The Phase 4 persistence service now delegates to this graph, preserving its
  existing result contract and eliminating duplicate orchestration code.
- Finalization recognizes an already committed matching terminal state on replay,
  preventing duplicate findings after commit-before-checkpoint failure.
- `scripts/review_workflow.py`: free mock graph run, optional durable persistence,
  a static debugging pause before persistence, and explicit resume by review UUID.
- README graph/demo updates and `docs/WORKFLOW.md` state/recovery/transaction guide.

Verified on Python 3.11.9:

- Default suite: 230 passed, 2 skipped.
- With PostgreSQL 18: 269 passed, 2 skipped. Skips are SQLite variants of concurrency
  and durable-checkpointer tests; both PostgreSQL counterparts pass.
- New tests cover stage ordering, JSON-only state, safe agent failure, cancellation,
  checkpoints and thread/mode/version checks, deduplication, DB retry limits, and
  ingestion through mocked GitHub. Existing Phase 4 persistence tests still pass.
- Commit-before-checkpoint failure was simulated: resume kept one finding and did
  not repeat the completed model call.
- Actual PostgreSQL saver closed/reopened its connection and rebuilt the graph
  before resume, without re-running security.
- Separate CLI processes paused and resumed review
  `00000000-0000-4000-8000-000000000005`. A repeat resume reused the result.
  Real Uvicorn HTTP reads returned PARTIAL, risk score 4.5, one finding, HTTP 200.
- `alembic check` found no business-schema drift after checkpoint table setup.
- Ruff lint/format and installed dependency compatibility checks passed.
- The current dependency resolution includes HTTPX2, resolving the earlier
  upstream Starlette test-client warning. No paid LLM calls/GitHub writes occurred.

Verification uses the separate temporary PostgreSQL cluster under `.venv`, never
the existing Windows PostgreSQL service. It is stopped after checks. Docker builds
and real provider account/model behavior remain unverified.

Resume skips successfully checkpointed stages; an unfinished node may run again.
This is not a distributed exactly-once guarantee. One caller per review thread is
required. Worker leases, queue consumption/acknowledgement, status synchronization,
automatic retries across jobs, and checkpoint retention remain later work. This
phase does not publish GitHub comments or perform semantic retrieval.

Workflow state is currently schema version 1. Review checkpoint compatibility when
changing topology/state in Phase 6; do not silently reinterpret incompatible saved
threads. `--resume` preserves the original mock/persistence modes and revision data.

## Next phase

Phase 6 adds Quality, Testing, and Documentation Agents with distinct prompts and
structured outputs, concurrent graph branches, shared findings, and partial-result
aggregation when one reviewer fails. Preserve controlled provider/output retries,
safe error data, checkpoint boundaries, and idempotent final persistence. PGVector
retrieval and Redis worker processing remain Phases 7 and 8.
