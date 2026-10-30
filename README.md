# AI Pull Request Review Agent

An automated GitHub pull request reviewer built with FastAPI,
LangGraph, specialized AI agents, PostgreSQL/PGVector, and Redis. The goal is a small,
understandable portfolio project with a working demo and defensible engineering choices.

**Current implementation: Phase 11 complete — operational polish and reproducible demos.** Security,
Quality, Testing, and Documentation Agents run in parallel between context preparation
and deterministic aggregation. Failed reviewers preserve successful results as a
partial review. Optional context retrieval supplies bounded related snippets from
an index of the PR's exact base commit. Standalone demos use
in-memory checkpoints; persisted workflows use PostgreSQL checkpoints and can
resume in another process. Signed webhooks, Redis acceptance, Alembic persistence,
and read APIs remain available. A separate worker now consumes accepted webhooks,
tracks status, caches revision-specific PR data, and resumes interrupted work.
Opt-in publishing adds one readable comment per saved review, durable write tracking,
revision checks, and read-only recovery of ambiguous writes. Publishing is disabled
by default, and mock/unknown legacy reviews are excluded by default.
The test suite now verifies signed deliveries through final comments with mocked
HTTP services, plus an opt-in PostgreSQL/PGVector and Redis matrix.
The original requirements are preserved in [docs/PRD.md](docs/PRD.md).

## Try the free demo

After installing the project (`python -m pip install -e ".[dev]"`):

```powershell
.\.venv\Scripts\python.exe scripts/demo.py
```

Open `artifacts/demo/review.md` for a complete four-agent fixture review and
`artifacts/demo/limited.md` for a partial review with explicit coverage limits.
The command also saves actual JSON workflow reports and correlated stage logs.
It uses no GitHub token, database server, or paid model. Checked-in previews:
[complete review](examples/demo/review.md), [limited review](examples/demo/limited.md).

[Demo and load simulation](docs/DEMO.md) · [Testing](docs/TESTING.md) ·
[Operations and retry policy](docs/OPERATIONS.md) · [Worker recovery](docs/WORKER.md) ·
[Publishing](docs/PUBLISHING.md)

The follow-on [Docker deployment check](docs/DEPLOYMENT.md) builds and verifies an
isolated Linux-container stack, including the API, worker, PostgreSQL/PGVector,
Redis, and fixture-only publishing. Run it with
`.\.venv\Scripts\python.exe scripts/compose_smoke.py` while Docker is running.

## Features

- Signed webhook acceptance, delivery deduplication, asynchronous Redis jobs,
  receipt recovery, and temporary revision-scoped PR caching.
- Parallel Security, Testing, Quality, and Documentation reviewers with schema
  validation, diff evidence checks, bounded repair, and independent failure handling.
- Optional exact-base repository context via PGVector, deterministic deduplication
  and weighted risk, persisted findings, and review/status APIs.
- Guarded Markdown publication with durable write intent and conservative
  reconciliation of ambiguous comment writes.
- Correlated JSON timing logs, prioritized bounded diff selection, complete/partial
  demo artifacts, and a measured mock-load harness.

## Tech stack

Python 3.11+, FastAPI/Pydantic, HTTPX, LangGraph, SQLAlchemy/Alembic,
PostgreSQL/PGVector, Redis, and Docker Compose. Pytest, Ruff, SQLite, and fakeredis
support repeatable local verification. LLM and embedding adapters use configurable
compatible endpoints; deterministic mock providers make demos free.

## Architecture

Primary deployed flow (retrieval and publishing are configurable):

```mermaid
flowchart TD
    GitHub[Signed GitHub PR webhook] --> API[FastAPI: verify and accept]
    API --> Queue[Redis queue, receipts, status]
    Queue --> Worker[Worker: lease and PostgreSQL review lock]
    Worker --> Ingest[LangGraph ingest: fetch pinned PR revision]
    Ingest --> Context[Select bounded diff and optional context]
    Index[(Exact-base PGVector index)] --> Context
    Context --> Security[Security]
    Context --> Testing[Testing]
    Context --> Quality[Quality]
    Context --> Documentation[Documentation]
    Security --> Aggregate[Deduplicate and compute risk]
    Testing --> Aggregate
    Quality --> Aggregate
    Documentation --> Aggregate
    Aggregate --> Save[(PostgreSQL review and findings)]
    Save --> Publish[Guarded publication and reconciliation]
    Publish --> Comment[GitHub Markdown comment]
    Publish --> Ack[Redis acknowledgement or deferred retry]
    Reads[Review and preview APIs] --> Save
```

The webhook returns HTTP 202 before model work. Successful sibling results survive
individual reviewer failures. Saving analysis precedes optional comment creation
and queue acknowledgement; crash recovery reuses durable outcomes. See
[OPERATIONS.md](docs/OPERATIONS.md) for limits, retries, and recovery boundaries.

Dependency readiness:

```mermaid
flowchart LR
    Client[Client / Swagger] --> API[FastAPI GET /health]
    API --> DB[PostgreSQL: query vector extension]
    API --> Redis[Redis: PING]
    DB --> Result[200 healthy / 503 degraded]
    Redis --> Result
```

The two checks run concurrently, each with a configurable deadline. Application
lifespan creates the connection pools and closes them on shutdown. Connections are
established lazily: an unavailable dependency produces a degraded health response,
while Swagger remains accessible.

Phase 2 webhook flow:

```mermaid
flowchart LR
    GitHub[Signed GitHub webhook] --> Verify[Verify raw-body HMAC]
    Verify --> Validate[Validate event and target PR]
    Validate --> Queue[Redis: deduplicate / enqueue job]
    Queue --> Accepted[HTTP 202 + review_id]
```

The endpoint makes no GitHub or LLM requests. A small Redis script atomically
deduplicates delivery IDs, stores expiring job/status metadata, and appends the
full job to `reviews:pending`. This introduces the minimum queue boundary required
for a quick webhook response. The Phase 8 worker claims these jobs, fetches the
queued revision, and runs the durable graph. Redis receipts, heartbeats, delayed
retries, and PostgreSQL review locks coordinate recovery and multiple workers.

Phase 3 adds a separate persistence/read path: a caller saves a validated job and
PR context through `ReviewRepository`, completes it with findings in one explicit
transaction, and reads the result through FastAPI. The synthetic demo below
exercises this path. Webhook acceptance writes only to Redis; the worker's ingestion
step creates PostgreSQL records. The status endpoint is available before ingestion.

The manual commands are such callers. They accept normalized PR JSON, run the
specialized agents through the configured provider, and can persist the outcome for
the existing read API. This is a manually invoked workflow; it does not
consume webhook jobs or publish to GitHub.

```mermaid
flowchart LR
    Source[Normalized PR JSON / GitHub service] --> Ingest[1. Ingest]
    Ingest --> Context[2. Prepare diff and optional repository context]
    Index[(Revision-scoped PGVector index)] --> Context
    Context --> Security[3a. Security Agent]
    Context --> Quality[3b. Quality Agent]
    Context --> Testing[3c. Testing Agent]
    Context --> Documentation[3d. Documentation Agent]
    Security --> Aggregate[4. Wait for all / deduplicate / risk]
    Quality --> Aggregate
    Testing --> Aggregate
    Documentation --> Aggregate
    Aggregate --> Persist[5. Persist outcome]
    Persist --> Read[Review API]
    Ingest -. checkpoints .-> Checkpoints[(Memory / PostgreSQL)]
    Security -. checkpoints .-> Checkpoints
    Persist -. checkpoints .-> Checkpoints
```

All four reviewers run by default. Context preparation reports `diff_only` or
`pgvector` when retrieval is enabled. The final graph node persists a result; the
worker optionally publishes it before acknowledgement. See [the workflow guide](docs/WORKFLOW.md) for state,
failure, transaction, and recovery details.

## Project structure

```text
app/
  main.py                 # App factory and lifespan resource management
  api/routes/health.py    # Concurrent dependency readiness checks
  api/routes/webhooks.py  # Bounded body, signature validation, quick job acceptance
  api/routes/reviews.py   # Stored review, findings, and repository-history reads
  api/routes/jobs.py      # Redis status, attempts, safe errors, retry scheduling
  api/routes/publications.py # Read-only Markdown preview and publication status
  api/routes/repositories.py # Bounded revision-pinned repository indexing
  core/config.py          # Environment and .env configuration
  core/logging.py         # JSON timing logs with per-review/agent context
  core/security.py        # HMAC-SHA256 signature verification
  core/exceptions.py      # Sanitized GitHub API failures
  db/session.py           # Async SQLAlchemy engine configuration
  db/models.py            # Review data, repository indexes, and vector chunks
  db/repositories.py      # Transaction-scoped review persistence and queries
  db/context_repository.py # Atomic snapshot replacement and PGVector cosine search
  schemas/github.py       # GitHub payloads and normalized PullRequestContext
  schemas/jobs.py         # Queue contract and webhook responses
  schemas/review.py       # Findings, completion contracts, enums, API responses
  schemas/agents.py       # Security output and agent/run results
  agents/                # Shared validation plus four specialized reviewers
  graph/state.py          # JSON-only shared state, reducers, safe result report
  graph/nodes.py          # Five node implementations and transaction boundaries
  graph/workflow.py       # Graph edges, bounded retries, start/resume/report
  graph/checkpoints.py    # Strict PostgreSQL checkpointer lifecycle
  prompts/security.py     # Security-only responsibilities and evidence instructions
  services/github_service.py # GitHub reads, pagination, retries, comment API
  services/job_queue.py   # Atomic delivery deduplication and enqueue
  services/worker_queue.py # Token-owned receipts, leases, delayed retry, recovery, ACK
  services/pr_cache.py    # Revision/credential-scoped temporary PR context
  services/comment_renderer.py # Bounded human-readable review Markdown
  services/publication_service.py # Durable outgoing intent, revision checks, reconciliation
  worker/                # Separate worker CLI, workflow processing, PostgreSQL ownership
  services/llm_service.py # Provider protocol, compatible HTTP adapter, mock provider
  services/review_service.py # Single-agent persistence and deterministic risk
  services/aggregation_service.py # Lightweight deduplication and confidence-weighted risk
  services/embedding_service.py # Compatible embeddings and free lexical mock
  services/indexing_service.py # File selection, line-anchored chunks, batched indexing
  services/context_service.py # Exact-base retrieval and bounded context
docker/postgres/init.sql  # Enable vector in a fresh database
examples/pull_request.opened.json # Sample webhook, with fictional commit SHAs
examples/security_pr.json # Synthetic normalized security-review fixture
scripts/send_webhook.py   # Signed local webhook demo
scripts/fetch_pr.py       # Read a real PR as normalized JSON
scripts/seed_demo.py      # Persist a labeled synthetic review without an LLM
scripts/review_security.py # Run the Security Agent, optionally persist its result
scripts/review_workflow.py # Run, pause, or resume the checkpointed workflow
scripts/index_repository.py # Index GitHub source or a normalized source fixture
scripts/test_suite.py     # Lint, formatting, tests; optional service matrix
scripts/demo.py           # Complete/limited mock reviews and correlated event artifacts
scripts/simulate_load.py  # Signed ASGI acceptance, background draining, saved-review reads
alembic/                  # Async migration environment and versioned schema
alembic.ini
tests/
  conftest.py             # Configuration/HTTP isolation and shared fixtures
  support/                # Migrated databases and signed GitHub fixture helpers
  unit/                   # Config, health, signatures, schemas, GitHub client, queue
  integration/            # Signed webhook to Redis; queued job to mocked GitHub context
  e2e/                    # Signed delivery through final published fixture comment
Dockerfile
docker-compose.yml
.env.example
pyproject.toml
```

## Running with Docker Compose

Install Docker with Compose support. On Windows, Docker Desktop must be running
with Linux containers enabled. From the project directory:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://localhost:8000/health
```

The Compose defaults also work without a `.env` file. On macOS/Linux, use
`cp .env.example .env` and `curl -i http://localhost:8000/health`.

- API: <http://localhost:8000>
- Swagger: <http://localhost:8000/docs>
- OpenAPI: <http://localhost:8000/openapi.json>
- PostgreSQL: `localhost:5432`, database/user/password: `review_agent`
- Redis: `localhost:6379`, database `0`

Compose waits for PostgreSQL and Redis readiness before starting the API. The
API container runs `alembic upgrade head` before starting Uvicorn and stops if
migration fails. This is a simple single-API local setup; future deployments with
multiple API replicas should run migrations once as a separate deployment step.
The PGVector image provides the extension; the initialization script enables it in
the application's database. PostgreSQL data survives container restarts in a named
volume. Redis now uses an append-only file and a named volume for queued jobs.
The default Redis append-only fsync policy can lose roughly the last second of
writes after a machine crash; acceptance is not an exactly-once guarantee.
Published ports bind to the local machine. The included credentials are local
development defaults.

Expected healthy response (HTTP 200):

```json
{
  "status": "healthy",
  "dependencies": {
    "postgres": "up",
    "pgvector": "up",
    "redis": "up"
  }
}
```

Inspect logs and stop the stack:

```powershell
docker compose logs -f api
docker compose down
```

If port 5432, 6379, or 8000 is already occupied, change its host-side mapping in
`docker-compose.yml`. Container-to-container addresses stay the same. When running
Python on the host, update `.env` for any changed database/Redis host ports.

The database init script only runs on a fresh data volume. For an existing database
missing the extension, enable it without deleting data:

```powershell
docker compose exec postgres psql -U review_agent -d review_agent -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

## Running Python locally

Python 3.11+ is required. Start infrastructure with Compose, then run the API on
the host (do not also start the Compose API on the same port):

```powershell
docker compose up -d postgres redis
Copy-Item .env.example .env
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

For macOS/Linux, create the environment with `python3 -m venv .venv` and use
`.venv/bin/python` instead. Run commands from the repository root so `.env` is found.
Tests can run without Docker, PostgreSQL, Redis, GitHub credentials, or LLM credits.
Without infrastructure, the API still starts and `/health` correctly returns 503.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_NAME` | `AI Pull Request Review Agent` | Swagger application title |
| `LOG_LEVEL` | `INFO` | Application log level |
| `DATABASE_URL` | Local async PostgreSQL URL in `.env.example` | Must use `postgresql+asyncpg://` |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `HEALTH_CHECK_TIMEOUT_SECONDS` | `3` | Per-dependency deadline; greater than 0, at most 30 |
| `GITHUB_WEBHOOK_SECRET` | Empty | Shared HMAC secret; empty disables webhook acceptance |
| `GITHUB_TOKEN` | Empty | Optional token for public reads; required for private repositories/writes |
| `GITHUB_API_BASE_URL` | `https://api.github.com` | REST API base URL; can point to an enterprise API |
| `GITHUB_API_VERSION` | `2026-03-10` | Explicit GitHub API version |
| `GITHUB_REQUEST_TIMEOUT_SECONDS` | `15` | HTTP connection/read/write/pool timeout |
| `GITHUB_MAX_ATTEMPTS` | `3` | Total GET attempts, from 1 to 3 |
| `GITHUB_MAX_FILE_BYTES` | `1000000` | Maximum decoded text file size, at most 1 MB |
| `MAX_WEBHOOK_BODY_BYTES` | `2000000` | Maximum body size, enforced while streaming |
| `WEBHOOK_ENQUEUE_TIMEOUT_SECONDS` | `3` | Redis acceptance deadline, at most 5 seconds |
| `REVIEW_JOB_TTL_SECONDS` | `86400` | Status/job metadata and delivery deduplication lifetime |
| `LLM_MOCK_MODE` | `true` | Use predefined responses, with no LLM client or credits |
| `LLM_BASE_URL` | Empty | Compatible API base, including its `/v1` prefix if required |
| `LLM_MODEL` | Empty | Provider model name; required only in real mode |
| `LLM_API_KEY` | Empty | Bearer token; may be empty for a local unauthenticated provider |
| `LLM_OUTPUT_MODE` | `json_schema` | Strict schema response format, or explicit `json_object` compatibility |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `30` | HTTP timeout and overall deadline for each request |
| `LLM_MAX_ATTEMPTS` | `3` | Total transport attempts per completion, from 1 to 3 |
| `AGENT_TIMEOUT_SECONDS` | `210` | Whole-reviewer graph deadline, greater than 0 and at most 600 |
| `LLM_MAX_OUTPUT_TOKENS` | `2048` | Output token budget (128–8192) |
| `LLM_TOKEN_LIMIT_FIELD` | `max_completion_tokens` | Set `max_tokens` for providers using that field |
| `MAX_FILES_PER_REVIEW` | `30` | Maximum supplied textual diffs |
| `MAX_PATCH_CHARS` | `60000` | Total patch-character budget across those files |
| `RETRIEVAL_ENABLED` | `false` | Opt in to indexed base-commit context in the workflow CLI |
| `EMBEDDING_MOCK_MODE` | `true` | Free lexical demonstration embeddings |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` | empty | Explicit compatible provider configuration |
| `EMBEDDING_API_KEY` | empty | Separate embedding provider credential, when required |
| `EMBEDDING_DIMENSIONS` | `128` | Expected output dimensions, configurable 8–2000 |
| `MAX_CONTEXT_CHUNKS` / `MAX_CONTEXT_CHARS` | `3` / `6000` | Maximum retrieved snippets and total snippet characters |
| `WORKER_MAX_ATTEMPTS` / `WORKER_RETRY_SECONDS` | `3` / `10` | Workflow execution budget and initial infrastructure backoff |
| `WORKER_LEASE_SECONDS` | `30` | Receipt ownership, renewed every third of this interval |
| `WORKER_JOB_TIMEOUT_SECONDS` | `600` | Whole workflow execution deadline |
| `PR_CACHE_TTL_SECONDS` / `PR_CACHE_MAX_BYTES` | `300` / `500000` | Temporary revision-scoped PR context cache |
| `GITHUB_PUBLISH_ENABLED` | `false` | Explicitly enable GitHub comment writes |
| `GITHUB_PUBLISH_MOCK_REVIEWS` | `false` | Permit deliberate mock/unknown legacy review publication |
| `GITHUB_PUBLISH_MAX_ATTEMPTS` / `GITHUB_PUBLISH_TIMEOUT_SECONDS` | `3` / `60` | Safe automatic rounds and whole-publication deadline |

Environment variables override `.env`. Compose supplies service hostnames `postgres`
and `redis`; host Python uses `localhost`. Embedding, indexing, and retrieval settings
are active; see [the complete context limits](docs/CONTEXT.md). Neither model nor
embedding configuration causes the webhook handler to run external analysis.
See [worker configuration](docs/WORKER.md) for polling, shutdown, Redis deadlines,
maintenance/dead-letter bounds, and recovery behavior.
Publication permissions, body/history limits, and unknown-write recovery are
documented in [the publishing guide](docs/PUBLISHING.md).
Keep secrets in an untracked `.env`; never commit real tokens.

## API endpoints

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/health` | 200 if PostgreSQL, vector extension, and Redis are available; otherwise 503 |
| POST | `/webhooks/github` | Verify a signed delivery, enqueue supported PR events, return 202 |
| POST | `/repositories/index` | Index selected source at a resolved commit; bounded synchronous HTTP 200 with counts/coverage |
| GET | `/reviews/{review_id}/status` | Redis job status, attempts, safe error and retry time; 404 if unknown/expired |
| GET | `/reviews/{review_id}/comment` | Read-only Markdown preview of a completed/partial review |
| GET | `/reviews/{review_id}/publication` | Durable publication outcome, attempts, comment ID and safe reason |
| GET | `/reviews/{review_id}` | Stored review and findings; 404 if not persisted |
| GET | `/reviews/{review_id}/findings` | Findings for a stored review; 404 for unknown review |
| GET | `/repositories/{owner}/{repository}/reviews` | Case-insensitive history, `limit=20` (1–100), `offset=0` |
| GET | `/docs` | Interactive Swagger UI |
| GET | `/openapi.json` | OpenAPI schema |

Health responses expose dependency states without database errors or credentials.
Application logs record the failed dependency and exception type for diagnosis.

## Database design

```mermaid
erDiagram
    REPOSITORIES ||--o{ PULL_REQUESTS : contains
    PULL_REQUESTS ||--o{ REVIEWS : receives
    REVIEWS ||--o{ FINDINGS : contains
    REVIEWS ||--o| REVIEW_PUBLICATIONS : publishes
    REPOSITORY_INDEXES ||--o{ REPOSITORY_CHUNKS : contains
    REPOSITORIES {
        uuid id PK
        string github_full_name UK
        datetime created_at
        datetime updated_at
    }
    PULL_REQUESTS {
        uuid id PK
        uuid repository_id FK
        int pr_number
        string title
        string author
        string base_branch
        string head_branch
    }
    REVIEWS {
        uuid id PK
        uuid delivery_id UK
        uuid pull_request_id FK
        string status
        string base_sha
        string head_sha
        bool mock_mode
        string risk
        float risk_score
        string summary
        string recommendation
        datetime completed_at
    }
    FINDINGS {
        uuid id PK
        uuid review_id FK
        string agent_type
        string severity
        string file_path
        int line_number
        float confidence
    }
    REPOSITORY_INDEXES {
        uuid id PK
        string repository
        string commit_sha
        string embedding_identity
        int embedding_dimensions
        bool limited
        datetime indexed_at
    }
    REPOSITORY_CHUNKS {
        uuid id PK
        uuid index_id FK
        int ordinal
        string file_path
        int start_line
        int end_line
        text content
        vector embedding
    }
    REVIEW_PUBLICATIONS {
        uuid review_id PK
        string status
        text body
        string destination_hash
        bigint author_id
        bigint comment_id
        int attempts
        int post_attempts
        datetime next_retry_at
        datetime published_at
    }
```

Repository identifiers are stored lowercase because GitHub names are case
insensitive. A unique `(repository_id, pr_number)` constraint identifies each PR.
Review IDs reuse queue job IDs, and delivery IDs are unique. Replaying the same
job is idempotent; conflicting IDs or mismatched job/context revisions are rejected.
PR metadata can update while each review retains its original base/head SHAs.

Database checks enforce status/severity/recommendation values, positive PR and
line numbers, confidence in `[0, 1]`, nonnegative risk scores, and complete terminal
records. Foreign keys cascade when a parent record is removed. History and
finding lookups have supporting indexes. Text/check-based enums keep migrations
simple without separate PostgreSQL enum-type lifecycle management.

The repository never commits implicitly. Callers use an explicit transaction:

```python
async with session_factory.begin() as session:
    store = ReviewRepository(session)
    review = await store.create_review(job, context)
    await store.mark_processing(review.id)
    await store.complete_review(review.id, result)
```

For a long review, commit ingestion first, run external work outside the database
transaction, then use a fresh transaction for completion. Valid transitions are
`QUEUED → PROCESSING → COMPLETED/PARTIAL`, or `QUEUED/PROCESSING → FAILED`.
Row locks serialize state changes on PostgreSQL. Terminal results cannot be
overwritten through these methods. Completion and all findings commit or roll back
together. Savepoints handle concurrent repository/PR creation. Queries eagerly
load required relationships; other relationship access raises instead of causing
unexpected async database I/O.

Migration `0002` adds repository snapshot metadata and vector chunks. Snapshot keys
isolate repository, commit SHA, and embedding profile. Index replacement is atomic,
and source/embedding I/O takes place outside database transactions. Retrieval uses
exact PGVector cosine search within the matching PR base snapshot. The migration
enables the extension when necessary; its binaries must be installed on the server.
Migration `0003` records review execution mode and adds the publication outbox.
Legacy modes remain unknown. Publishing has a separate lifecycle from analysis;
GitHub failures cannot erase a completed review.

## Phase 3 persistence demo

Start Compose, then seed a synthetic result and read the ID printed by the command:

```powershell
docker compose up --build -d
docker compose exec api python scripts/seed_demo.py
Invoke-RestMethod http://localhost:8000/reviews/028594a3-7329-50a1-84b6-98c97dbefcc4
Invoke-RestMethod http://localhost:8000/reviews/028594a3-7329-50a1-84b6-98c97dbefcc4/findings
Invoke-RestMethod http://localhost:8000/repositories/octocat/hello-world/reviews
docker compose exec api alembic current
```

The seed uses a fixed UUID, so rerunning it does not duplicate records. Its summary
explicitly identifies it as a synthetic persistence demo. It does not consume the
queue, call GitHub, or generate an AI review. You can also inspect these endpoints
in Swagger. A queued database review returns null risk/result fields and an empty
finding list. Unknown review IDs return 404; invalid IDs/pagination return 422;
database outages return a sanitized 503. Stored internal failure messages are
not exposed in the read response.

For host Python, run migrations and seed before starting the API:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe scripts/seed_demo.py
.\.venv\Scripts\python.exe -m alembic check
```

Alembic reads `DATABASE_URL` from the same environment configuration as the API;
the INI file contains no credentials. Do not use `metadata.create_all()` to manage
the application schema. Future schema changes should get a new migration:

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "Describe schema change"
```

Review generated migrations before applying them. The initial migration's downgrade
removes the four review tables and their data; normal startup only upgrades.

## Phase 4 Security Agent demo

After installing the package and development dependencies, run a free standalone demo:

```powershell
.\.venv\Scripts\python.exe scripts/review_security.py --mock
```

This reads `examples/security_pr.json` and prints a typed agent result. It needs no
database, Redis, GitHub token, or LLM credentials. `--mock` forces mock mode even
when the environment selects real mode. The predefined demo response reports the
fixture's SQL interpolation example; other supplied diffs receive a predefined
empty response unless they contain that exact demo filename/snippet. **Mock mode
is a fixture runner, not a general vulnerability detector.** Findings and summaries
are labeled as mock output; runtime timing metadata naturally varies.

To persist the security-only result and read it through FastAPI:

```powershell
docker compose up --build -d
docker compose exec api python scripts/review_security.py --mock --persist --review-id 00000000-0000-4000-8000-000000000004
Invoke-RestMethod http://localhost:8000/reviews/00000000-0000-4000-8000-000000000004
```

For host Python, apply migrations first, then use the same script with `--persist`.
If no review ID is supplied, a fresh job/review ID is generated. Retrying the same
ID and PR revision reuses an already terminal stored result without another model
call or duplicate findings. Conflicting revisions are rejected. This command is
intended for one caller per review ID. Do not manually run/resume an active
worker-owned ID; the worker coordinates its own callers through leases and DB locks.

The demo persists `PARTIAL`, `HIGH`, risk score `4.5`, `REQUEST_CHANGES`, and one
security finding with a `[MOCK]` summary. `PARTIAL` is intentional: testing, quality,
and documentation reviewers have not run. An agent's `COMPLETED` status means that
specific agent returned valid output; it is distinct from the stored overall status.

To use normalized JSON from a real PR, retrieve it first and pass its file path:

```powershell
.\.venv\Scripts\python.exe scripts/fetch_pr.py OWNER/REPOSITORY PR_NUMBER > pr-context.json
.\.venv\Scripts\python.exe scripts/review_security.py pr-context.json --mock
```

The fetch command contacts GitHub. The second command still uses fixtures until
real LLM mode is explicitly configured. With Windows PowerShell 5, write redirected
JSON as UTF-8 instead of its default UTF-16, for example:
`.\.venv\Scripts\python.exe scripts/fetch_pr.py OWNER/REPOSITORY PR_NUMBER | Set-Content -Encoding UTF8 pr-context.json`.
The Security Agent command accepts UTF-8 files with or without a byte-order mark.

## LLM provider configuration

For a real review, set `LLM_MOCK_MODE=false`, `LLM_BASE_URL` to your provider's API
base URL, `LLM_MODEL` to its supported model name, and `LLM_API_KEY` if required.
Then run the command without `--mock`. No model is hardcoded. Real mode deliberately
fails if the base URL or model is missing; it does not silently fall back to mock data.

The supplied adapter posts to `{LLM_BASE_URL}/chat/completions` using an explicit
JSON Schema response format. For compatible providers supporting only JSON-object
mode, set `LLM_OUTPUT_MODE=json_object`; the schema is then included in the system
prompt and all output is still validated locally. Set
`LLM_TOKEN_LIMIT_FIELD=max_tokens` if required by the provider. Unsupported request
parameters produce a controlled failure rather than an automatic compatibility
downgrade. Local providers may omit the API key. A non-compatible API can implement
the small `LLMProvider.generate(LLMRequest) -> str` protocol and be injected without
changing the agent.

The provider retries timeouts, network errors, HTTP 429, and server errors at most
`LLM_MAX_ATTEMPTS` total attempts. Backoff is 0.5 then 1 second. Numeric `Retry-After`
delays up to 5 seconds are respected; longer or unsupported delays return a
controlled failure. Typed agent failures are final graph outcomes rather than
automatic new worker executions. Permanent HTTP
errors, refusals, interrupted output, empty content, and invalid response envelopes
fail without repeated generation. API redirects are not followed. The agent can
request one additional completion for invalid output, so the worst-case default
budget per reviewer is **two completions × three transport attempts = six HTTP
requests**, or up to 24 across four reviewers. The graph also applies the
`AGENT_TIMEOUT_SECONDS` deadline independently to each reviewer, including repairs
and transport retries; reaching it produces a failed reviewer outcome.

## Security output and validation

The agent inspects security regressions supported by the supplied patches and
ignores code style, test coverage, and documentation. Its separate prompt allows
zero findings, distinguishes severity from confidence, treats repository text as
untrusted data, asks for concrete suggestions, and prohibits invented code or
internal chain-of-thought in the output. Repository code is never executed.

`SecurityReview` contains `findings`, `summary`, and an advisory integer `risk_score`
from 0 to 100. Each security finding uses the existing common persistence fields
plus a short exact `evidence` snippet. Strict schemas reject unknown properties and
require every property, including nullable `line_number`. The agent verifies that:

- The finding's category is security and its filename was actually supplied.
- Evidence occurs in the cited patch after diff markers are removed.
- A non-null line citation is an added new-file line in the provided hunk; use null
  for a deletion or an issue without a reliable added-line anchor.
- Severity, confidence, risk, and required fields pass Pydantic validation.

On schema or evidence failure, one repair completion receives the original bounded
input and validation error types, without echoing raw output or unknown field names.
A second failure becomes a typed failed agent result. Provider failures also become
failed results. Optional persistence records `FAILED` and a sanitized error message.
Logs contain stage/agent, repository, PR, duration, and status, not prompts or raw output.

Input is bounded to the configured file count and total patch budget. Titles and
descriptions, warning lists, and unusually long paths are also bounded. Missing
patches, truncation, and incomplete ingestion produce coverage warnings. With no
textual patches, the provider is skipped and the limitation is explicit. These
checks improve citation hygiene; they do not prove a finding is a real vulnerability
or guarantee that a model ignores malicious instructions in source text.

The single-agent persistence path calculates `sum(severity_weight × confidence)`
using `LOW=1`, `MEDIUM=2`, `HIGH=5`, `CRITICAL=10`. Score thresholds are 1 for medium,
4 for high, and 8 for critical. Scores at least 4 recommend `REQUEST_CHANGES`;
other security-only results recommend `COMMENT`, including zero findings. The
model's advisory score does not choose the stored risk or recommendation. The full
workflow uses the same weights after deduplicating findings across all reviewers.

Ingestion commits before model I/O. The agent runs with no open database
transaction, and completion/findings or failure persist in a separate transaction.
Raw agent evidence stays in the returned agent result; the database stores the
existing common finding fields. The Phase 4 persistence service now delegates to
the shared five-stage graph. Its convenience command uses in-memory checkpoints;
use the workflow command below for durable cross-process recovery.

## Phase 6 multi-agent demo

Each reviewer has a distinct prompt and category-specific output schema:

| Reviewer | Focus |
| --- | --- |
| Security | Exploitable regressions such as injection and authorization errors |
| Quality | Concrete correctness, maintainability, and performance problems |
| Testing | Specific missing regression cases supported by changed behavior |
| Documentation | User-facing behavior or configuration needing explanation |

All share bounded input, strict validation, exact diff evidence checks, and at most
one repair completion. Testing and Documentation qualify gaps against the supplied
context rather than assuming the rest of the repository has no tests or docs.
No reviewer executes repository code.

Run the complete graph locally without infrastructure or credits:

```powershell
.\.venv\Scripts\python.exe scripts/review_workflow.py --mock
```

The default fixture is `examples/multi_agent_pr.json`. Its report shows eight
completed nodes (including four parallel reviewers), four findings, `COMPLETED`,
risk score `7.7`, `REQUEST_CHANGES`, and `persisted: false`. Each reviewer includes
its status, attempts, timing, and validated output. The mock provider returns
predefined fixture responses; it is not a general code analyzer.
In this mode the final node returns the result without writing
to a database, and checkpoints last only as long as that process.

For a durable pause/resume demo with Compose:

```powershell
docker compose up --build -d
docker compose exec api python scripts/review_workflow.py --mock --persist --review-id 00000000-0000-4000-8000-000000000006 --stop-before-persist
docker compose exec api python scripts/review_workflow.py --mock --persist --resume 00000000-0000-4000-8000-000000000006
Invoke-RestMethod http://localhost:8000/reviews/00000000-0000-4000-8000-000000000006
```

The first process stops after aggregation with `paused: true`, `status: PROCESSING`,
and `pending_nodes: ["persist"]`. The second loads PostgreSQL checkpoints and
finishes the write without rerunning any checkpointed reviewer. It returns
`COMPLETED`, risk score `7.7`, four findings, and `persisted: true`. Here `persisted`
means the final application result, not the checkpoint itself. Repeating the resume
returns the stored result with `reused: true`; it does not add another finding.
Use a fresh UUID when repeating the pause demo as a new review.

For host Python, apply migrations, then replace the `docker compose exec api python`
prefix with `.\.venv\Scripts\python.exe`. The CLI chooses a selector event loop on
Windows because the PostgreSQL checkpoint driver's async API requires it.
The same `--mock` and `--persist` modes must be used when resuming a thread.

Full reviews use checkpoint schema version 2 and validate the reviewer set on
resume. For a Phase 5 checkpoint or a security-only demo, add `--security-only`
to both start and resume commands; this preserves the version 1 graph. An old
checkpoint cannot silently become a four-reviewer run.

`COMPLETED` means all four reviewers succeeded with full supplied diff coverage;
it can still recommend changes. Failed/omitted reviewers or coverage warnings
produce `PARTIAL`; all reviewers failing produces `FAILED`. Only a complete review
with zero findings can recommend `APPROVE`. A barrier waits for every branch before
aggregation. Successfully saved sibling outputs survive interruption and are reused
when a pending reviewer resumes.

Business tables are under Alembic revision `0003`. The official PostgreSQL
checkpointer manages its own internal tables in the separate `review_workflow`
schema, initialized by its `setup()` method. This keeps them outside business
schema autogeneration. The configured database role must be allowed to create the
schema and its tables. SQLAlchemy/asyncpg still owns business data; Psycopg 3 is
used by the official LangGraph checkpointer. No separate database service is added.

These commands manually invoke the same graph used by the Phase 8 worker.
Checkpoint recovery does not imply exactly-once execution: a node without durable
output can run again. Worker leases and PostgreSQL session locks coordinate worker
instances; manual callers must avoid active worker-owned review IDs.

## Phase 7 PGVector demo

After configuring a PostgreSQL/PGVector database in `.env`, run without paid calls:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe scripts/index_repository.py --mock --fixture examples/repository_index.json
.\.venv\Scripts\python.exe scripts/review_workflow.py --mock --persist --with-context
```

The workflow retrieves related test, database, and README snippets from the fixture's
exact base commit. Reports include `context_source=pgvector` and bounded snippets
with paths, revisions, line ranges, and similarity. All reviewers receive the same
supporting context; findings must still cite changed-file evidence. Mock embeddings
demonstrate lexical ranking, not semantic model quality.

Index a real repository through `POST /repositories/index` with
`{"repository":"OWNER/REPOSITORY","ref":"BASE_COMMIT_SHA"}`, or use
`scripts/index_repository.py OWNER/REPOSITORY --ref BASE_COMMIT_SHA --mock`.
This reads GitHub; the fixture command above does not. Missing matching indexes or
unavailable retrieval keep diff findings and mark coverage partial. Disabled
retrieval leaves the existing standalone demo available.

Retrieval-enabled checkpoints use version 3; resume with the same retrieval mode,
embedding profile, and budgets. Use `--diff-only` to select older full-review
checkpoints. See [repository context](docs/CONTEXT.md) for configuration, file policy,
provider setup, storage guarantees, and the durable pause/resume demo.

## Phase 8 worker

Compose now starts a separate worker after the API's healthy migration/startup.
For host Python with PostgreSQL/PGVector and Redis configured:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m app.worker --mock
```

The worker continuously processes accepted webhook jobs. `--mock` prevents paid
model/embedding calls; GitHub reads still use the real configured service. A real
PR payload must contain current base/head SHAs. The old fictional sample demonstrates
enqueueing and will fail ingestion when its revision cannot be verified.

Use `--once` for one maintenance/claim pass and at most one job. Add `--with-context`
for indexed base-commit retrieval, or `--diff-only` to disable it. Ctrl+C/SIGTERM
stops claims, gives in-flight work a grace period, then leaves checkpoints/receipts
for recovery. Publishing is off by default; Phase 9 can enable it explicitly.

```powershell
Invoke-RestMethod http://localhost:8000/reviews/REVIEW_UUID/status
Invoke-RestMethod http://localhost:8000/reviews/REVIEW_UUID
```

Status progresses QUEUED → PROCESSING → COMPLETED/PARTIAL/FAILED. Retriable
infrastructure failures return QUEUED with a retry time. Jobs rejected before
ingestion can be FAILED in Redis without a fabricated PostgreSQL review. Final
PostgreSQL results are reused when a final checkpoint or Redis ACK was interrupted.
Payloads in pending/processing/delayed structures survive expiring display metadata.
See [the worker guide](docs/WORKER.md) for ownership, retries, cache behavior,
dead letters, limits, and verification. Business migration is now `0003`.

## Phase 9 GitHub comments

Preview the labeled fixture without services or paid calls:

```powershell
.\.venv\Scripts\python.exe scripts/publish_review.py --demo
```

See [the example comment](examples/review_comment.md). Preview a persisted review
with `scripts/publish_review.py REVIEW_UUID` or `GET /reviews/REVIEW_UUID/comment`.
These previews perform no GitHub writes.

To enable the worker's publisher, set `GITHUB_PUBLISH_ENABLED=true` and configure
a GitHub token supporting `/user` with Pull requests read/write access. Keep
`GITHUB_PUBLISH_MOCK_REVIEWS=false` for normal operation. New reviews record their
real/mock mode; unknown legacy modes are skipped by default.

```powershell
.\.venv\Scripts\python.exe scripts/publish_review.py REVIEW_UUID --publish
Invoke-RestMethod http://localhost:8000/reviews/REVIEW_UUID/publication
```

The explicit publish command also requires the configuration flag/token and the
review ownership lock. The worker checks the current base/head SHAs and open PR
state immediately before writing. Frozen bodies and authenticated-author matching
reconcile uncertain writes. A timed-out POST is never blindly repeated; an absent
ambiguous comment remains UNCERTAIN for operator investigation. Publishing failure
leaves the saved review intact and has a separate outcome/retry budget.

See [publishing](docs/PUBLISHING.md) for permissions, limits, statuses, reconciliation,
and the remaining small race between GitHub's revision check and comment creation.

## Phase 2 webhook demo

For this historical enqueue-only walkthrough, stop the new consumer with
`docker compose stop worker`; restart it afterwards with `docker compose start worker`.

In `.env`, set `GITHUB_WEBHOOK_SECRET` to a randomly generated secret of your own.
For example, print a fresh secret locally with
`py -3.11 -c "import secrets; print(secrets.token_hex(32))"` and put that value in
the file. Do not commit it. Recreate the API after updating Compose environment:

```powershell
docker compose up --build -d
docker compose exec api python scripts/send_webhook.py
docker compose exec redis redis-cli LLEN reviews:pending
docker compose exec redis redis-cli LRANGE reviews:pending 0 0
```

The sender signs the exact sample JSON bytes. A successful response looks like:

```json
{
  "review_id": "a generated UUID",
  "status": "QUEUED",
  "duplicate": false
}
```

Check the returned ID's state with
`docker compose exec redis redis-cli GET review:YOUR_REVIEW_ID:status`.
To demonstrate delivery deduplication, send twice with the same ID:

```powershell
docker compose exec api python scripts/send_webhook.py --delivery-id 00000000-0000-4000-8000-000000000002
docker compose exec api python scripts/send_webhook.py --delivery-id 00000000-0000-4000-8000-000000000002
```

The second response returns the same `review_id`, sets `duplicate` to `true`,
and does not append a second queue entry. Different deliveries, including new
`synchronize` events, create new jobs. Deduplication expires after the configured
TTL; the queue itself has no TTL and contains each complete job. Pending entries
survive metadata expiry. A running worker consumes the queue automatically.
Redelivery returns the retained current status, which can already be terminal.

For an API running directly on the host, replace the sender command with
`.\.venv\Scripts\python.exe scripts/send_webhook.py`.

Supported events are `pull_request.opened`, `.synchronize`, and `.reopened`.
Signed `ping` and unsupported events/actions are acknowledged with HTTP 200 and
`status: ignored`. Missing/bad signatures return 403; invalid JSON returns 400;
invalid PR data or delivery UUID returns 422; oversized bodies return 413;
non-JSON content types return 415. An unconfigured secret or unavailable queue
returns 503, never a false acceptance. On an uncertain timeout, retry using the
same delivery ID. No GitHub requests run in the webhook handler.

For a real GitHub repository webhook, select **Settings → Webhooks → Add webhook**,
set an externally reachable HTTPS URL ending in `/webhooks/github`, select
`application/json`, enter the same secret, and subscribe to pull request events.
GitHub must be able to reach the application; `localhost` only serves the local demo.

## Fetching PR context

The service provides `get_pull_request()`, `get_pull_request_files()`,
`get_file_content()`, `get_repository_metadata()`, `get_pull_request_context()`,
and `post_review_comment()`. All GitHub HTTP access stays in this service.

Read a real PR, substituting its repository and number:

```powershell
docker compose exec api python scripts/fetch_pr.py OWNER/REPOSITORY PR_NUMBER
# Or, after installing the package locally:
.\.venv\Scripts\python.exe scripts/fetch_pr.py OWNER/REPOSITORY PR_NUMBER
```

This command reads GitHub and prints normalized JSON; it does not consume queued
jobs, run agents, or publish a comment. The sample webhook's commit SHAs are
fictional, so use a real PR for this command. Public PR reads can be unauthenticated;
private repositories need a token with repository access and Pull requests read
permission. File-content retrieval also needs Contents read permission. The comment
method requires write permission. The opt-in Phase 9 publisher calls it after a
successful/partial review is saved; this fetch command itself remains read-only.

`PullRequestContext` includes repository, PR number/title/description/author,
base/head branch and SHA, fork repository, changed files, patches, additions,
deletions, reported file totals, and coverage warnings. The client pages through
file results sequentially, up to GitHub's 3,000-file API limit, and preserves absent
patches as `null`. It marks context incomplete when reported file counts exceed
retrieved files. It checks the base/head SHAs before and after fetching files;
worker ingestion supplies a queued job's expected SHAs to reject a stale revision.

Temporary GET failures retry at most three total attempts, with 0.5/1-second
backoff. Permanent errors, rate limits, and server `Retry-After` responses fail
without early inline retries; the worker schedules supported retry/reset windows.
POSTs are not retried automatically because a timed-out comment request may
already have succeeded. File reads are bounded UTF-8 text reads at an explicit
revision. The client constructs trusted API paths rather than following payload
URLs, pagination targets, or redirects. It never executes repository code.

## Testing and verification

After installing the development dependencies:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Or run all three checks with `.\.venv\Scripts\python.exe scripts/test_suite.py`.
This defaults to local SQLite and simulated Redis even if service-test URLs are
inherited. See [docs/TESTING.md](docs/TESTING.md) for layer selection, service opt-in,
the requirement matrix, and test isolation. Native HTTPX requests are blocked in
pytest; application settings and implicit `.env` files are isolated from tests.

Tests cover configuration, health, signatures, payload validation, atomic enqueue,
delivery deduplication, metadata expiry, concurrent redelivery, queue outages and
deadlines, GitHub pagination, retries, rate limits, absent patches, file safety,
forks, and stale revisions. `httpx.MockTransport` mocks GitHub; `fakeredis[lua]`
executes the actual enqueue script against in-memory Redis. Database health I/O
is mocked. No GitHub credentials, real external writes, or LLM credits are required.
These pytest runs do not validate a Docker deployment. The separate
[Compose smoke check](docs/DEPLOYMENT.md) has now passed against actual Linux
containers. Its fixture/isolation helpers bring the default suite to **489 passed,
12 expected skips**; real GitHub and paid providers remain unverified.

Phase 3 persistence tests apply the actual Alembic migration to an isolated SQLite
file by default, with foreign keys and transactional savepoints enabled. They
exercise persistence across sessions, rollback, idempotency, unique constraints,
state transitions, cascades, schema drift, migration downgrade/upgrade, and real
database-backed API reads. SQLite is a test backend; application configuration
continues to require PostgreSQL.

To also run those tests against PostgreSQL, set `TEST_DATABASE_URL` in the test
process (it is not read from `.env` by pytest):

```powershell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://review_agent:review_agent@localhost:5432/review_agent"
.\.venv\Scripts\python.exe -m pytest
```

Use a development/test database and a role allowed to create schemas. Each test
creates a unique `test_<uuid>` schema, applies migrations there, and removes only
that schema during cleanup. Existing application tables are not used. PostgreSQL
also verifies concurrent duplicate ingestion; that specific case is skipped on SQLite.

Phase 11 local verification on Python 3.11.9: **483 passed, 12 skipped** without
external services; **608 passed, 11 skipped** with PostgreSQL 16.15 / PGVector 0.8.6
and Redis 7.4.11 enabled. The latter skips are SQLite variants of cases requiring
PostgreSQL locks/checkpoints/vector operations; their PostgreSQL counterparts pass.
The default suite additionally skips the real Redis check. Ruff lint,
format checks, and dependency compatibility checks passed.
A real Uvicorn process served `/docs` and `/openapi.json` with HTTP 200 and
returned the expected HTTP 503 when connected to unavailable PostgreSQL/Redis
addresses. A Phase 2 live HTTP check also ran the signed sender against Uvicorn
and a TCP in-memory Redis simulator, exercising the actual enqueue script:
HTTP 202 on acceptance, one queue entry and the same review ID on redelivery,
and HTTP 403 for a tampered signature. GitHub calls were mocked during verification.
Phase 3 also verified `alembic upgrade head`, `alembic check`, repeated seeding,
and real HTTP review/findings/history reads against a separate temporary local
PostgreSQL cluster. Earlier dependency resolutions emitted a Starlette HTTPX
warning; the current tested dependency set includes HTTPX2 and no longer emits it.
Docker is not installed on the implementation machine, so container builds and
Compose startup remain unverified. Phase 8 verified a healthy HTTP response
against the separate real local PostgreSQL/PGVector and Redis processes.

Phase 4 adds mock/no-network tests, provider wire-format and retry tests, structured
schema/repair/evidence checks, citation rejection, input budgets, deterministic
security scoring, and model-to-database integration. Provider HTTP is mocked with
`httpx.MockTransport`; no paid LLM requests run in automated tests. A live CLI and
HTTP check persisted a mock Security Agent result in PostgreSQL, verified it through
the review API, and confirmed repeated IDs reused the stored result. A standalone
mock CLI check also passed with database/Redis deliberately unavailable. Real LLM
account/model compatibility remains unverified; no paid LLM calls were made.

Phase 5 tests cover ordered graph transitions, JSON-only state, bounded database
retries, safe agent failure data, cancellation/resume, input/mode/thread/version
checks, lightweight deduplication, and replay after commit-before-checkpoint failure.
A real PostgreSQL checkpointer test closes and recreates both connection and graph
before resuming. Separate CLI processes also paused/resumed a real PostgreSQL
checkpoint; live HTTP reads confirmed one stored finding. `alembic check` reported
no business-schema drift after checkpoint table creation.

Phase 6 adds async-barrier concurrency checks, specialized schemas/prompts,
independent failure/deadline tests, all-failed handling, Unicode-safe deduplication,
and partial-result persistence/API reads. A real PostgreSQL checkpoint test
reopened the connection and resumed only the interrupted reviewer, reusing three
saved sibling outputs. Separate CLI processes completed and reused the four-agent
review and resumed an actual Phase 5 checkpoint with `--security-only`. Live HTTP
review/findings/history reads confirmed four categories, COMPLETED, and score 7.7.

Phase 7 tests cover embedding response/retry validation, revision-pinned GitHub
indexing, selection/budgets, atomic replacement/rollback/concurrent writers, real
PGVector ranking and scope isolation, safe API errors, bounded prompt injection,
deadline fallback, and durable context reuse after index removal. A live HTTP
indexing request used a loopback GitHub fixture server, persisted three chunks,
and fed a separate-process pause/resume workflow. Live review/findings/history
reads confirmed the result. Final `alembic check` found no schema drift; 72 installed
packages passed dependency checks. Paid providers and Docker remain unverified.

Phase 8 adds queue ownership/recovery, delayed retries, safe status reads,
cache validation, graceful cancellation, real advisory locks, retry exhaustion,
and synchronization of partial/failed/completed results. A live signed-webhook
run used separate API/worker processes and loopback GitHub/model fixtures. Killing
one worker with three saved reviewer outputs, restarting real Redis from AOF, and
starting another worker reran only the interrupted reviewer. PR cache reuse avoided
another file fetch; final HTTP reads returned four findings and score 7.7. No paid
API calls or GitHub writes occurred. See [worker verification](docs/WORKER.md).

Phase 9 adds formatter, publication lifecycle, revision/author/history checks,
mock/legacy suppression, safe retries, cancellation/deadlines, lost-response and
lost-record recovery, and worker publication retries without repeated reviewers.
Separate local API/worker/CLI processes posted only to a loopback GitHub fixture:
one comment per review, redelivery/reuse with no extra write, and read-only
reconciliation after a dropped successful response. A mock review was skipped.
Alembic `0003`/schema checks, lint/format, and dependency checks passed. Real GitHub
publishing and paid providers remain unverified; no real GitHub comment was posted.

For a real infrastructure check, start Compose and verify the healthy response.
Then stop Redis with `docker compose stop redis`: `/health` should return HTTP 503
with Redis `down`. Start it again with `docker compose start redis` and verify the
endpoint recovers. PostgreSQL/PGVector should remain `up` during that check.

## Design decisions

- **FastAPI and Pydantic:** typed HTTP contracts and configuration validation with
  generated API documentation.
- **SQLAlchemy and asyncpg:** asynchronous PostgreSQL connections, a small pool,
  stale-connection checks, bounded connection acquisition, and explicit caller-owned
  transactions. Alembic tracks schema changes; eager queries avoid implicit async I/O.
- **PostgreSQL with PGVector:** relational reviews and revision-scoped context in
  one database. Exact cosine search suits the bounded corpus; vector profiles stay
  separate by endpoint/model/dimensions and mock mode.
- **Redis:** a small script gives atomic job acceptance and delivery deduplication.
  Queue entries contain the whole job, so expiring status metadata cannot erase
  pending work. Token-owned receipts, delayed retries, heartbeats, PostgreSQL
  ownership, and a revision-scoped PR cache provide practical processing/recovery.
- **GitHub boundary:** normalized Pydantic objects keep external payload details
  out of future agents. SHA checks detect stale ingestion; bounded retries and
  trusted URL construction keep failures controlled.
- **Explicit readiness:** checking the vector extension catches incomplete database
  setup. Health failures do not prevent access to the API documentation.
- **LangGraph:** explicit nodes and typed state expose stage boundaries, failure
  recovery, and checkpoint reuse. PostgreSQL checkpoints survive process restarts;
  synchronous checkpoint durability records each completed step before moving on.
- **Four specialized agents:** separate category prompts keep responsibilities and
  evidence requirements clear. Parallel execution reduces elapsed review time;
  independent results preserve useful analysis when one reviewer fails.
- **Small dependency surface:** dependencies arrive in the phases that use them.
  The compatible provider reuses HTTPX; it needs no model
  SDK or LangChain wrapper yet. There are no placeholder reviewer implementations.
- **Probabilistic/deterministic boundary:** model output is validated against a
  schema and the supplied diff; application code deduplicates and determines risk.
  Missing or failed reviewer categories remain visible through partial status.

## Development plan

1. **Foundation (implemented):** FastAPI, settings, PostgreSQL/PGVector, Redis,
   Docker Compose, health endpoint.
2. **GitHub (implemented):** signature verification, webhook payload validation, GitHub client,
   normalized PR diff retrieval.
3. **Database (implemented):** repositories, pull requests, reviews, findings, Alembic migrations,
   transactional repository methods, and stored-review read APIs.
4. **Single agent (implemented):** security prompt, provider abstraction, validated output,
   one repair, deterministic mock mode, input limits, and optional persistence.
5. **LangGraph (implemented):** typed state, five nodes, conditional reuse, bounded
   database retries, deterministic aggregation, durable checkpoints, and resume.
6. **Multiple agents (implemented):** quality, testing, documentation; concurrent execution,
   isolated failures/deadlines, shared findings, and partial-result aggregation.
7. **PGVector (implemented):** revision-pinned indexing, batched embedding providers,
   atomic vector snapshots, cosine similarity, bounded injection, and checkpoint reuse.
8. **Redis processing (implemented):** separate worker, reliable receipts/leases,
   recovery, delayed retries, status API, and temporary revision-scoped PR cache.
9. **GitHub output (implemented):** bounded Markdown comment, opt-in worker/CLI publishing,
   durable outgoing intent, revision checks, author/body reconciliation, preview/status APIs.
10. **Workflow tests (implemented):** shared fixtures, HTTP/configuration isolation,
    signed webhook-to-comment scenarios, service matrix, and a repeatable test runner.
11. **Polish (implemented):** correlated stage/agent/retry logs, prioritized bounded
    diff windows, documented retry/recovery policy, generated demos, and recorded
    100/1,000-delivery mock-load simulations. See [DEMO.md](docs/DEMO.md).

## Limitations and future improvements

The application processes signed webhook jobs with optional indexed context and opt-in
GitHub output, reconciling Redis outcomes with persisted PostgreSQL results. This is
at-least-once processing; unfinished model work can repeat after a crash. Worker
ownership uses a standalone Redis deployment and PostgreSQL session locks, not a
distributed exactly-once or failover guarantee. Expired Redis display metadata is
not durable history; completed review reads remain available from PostgreSQL.
Ambiguous comment writes are reconciled without automatic reposting. A PR can move
after the final revision check because issue comments have no atomic commit guard.
Prioritized file and changed-window budgets make limited coverage explicit;
exhaustive chunked review and queue admission/backpressure remain future work.
Local Linux-container build/startup, migrations, review delivery, fixture publishing,
and Redis readiness recovery have passed the isolated Compose check. Production
deployment configuration still requires its own validation. The current credentials
and configuration target local development. Dependencies use compatible version
ranges; release deployments should use a pinned dependency resolution.

All eleven implementation phases are complete. The measured load results are
in-process mock simulations, described with their boundaries in
[the benchmark record](examples/benchmarks/README.md).
LLM reviews can produce
false positives, retrieval is approximate, diff-only analysis can miss runtime
behavior, and large PRs require bounded context. Human review and validation of
generated suggestions remain necessary.

## Implementation references

Connection ownership follows [FastAPI's lifespan guidance](https://fastapi.tiangolo.com/advanced/events/).
The Redis client uses the [documented async API and explicit cleanup](https://redis.io/docs/latest/develop/clients/redis-py/async/).
The database image and extension setup follow the [PGVector installation documentation](https://github.com/pgvector/pgvector#installation).
Webhook authentication follows [GitHub's signature validation guidance](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).
Pagination and file limits follow [GitHub's pull request API](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files).
The client explicitly sets a [supported GitHub API version](https://docs.github.com/en/rest/about-the-rest-api/api-versions).
Retry handling follows [GitHub's REST best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api).
Async database ownership follows [SQLAlchemy's session guidance](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).
The migration environment follows [Alembic's async cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic).
The compatible request schema follows [official OpenAI structured-output documentation](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat).
Checkpoint and resume behavior follows [LangGraph's checkpointer guide](https://docs.langchain.com/oss/python/langgraph/checkpointers).
The checkpoint connection and setup follow the [official PostgreSQL checkpointer](https://pypi.org/project/langgraph-checkpoint-postgres/).
