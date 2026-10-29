# AI Pull Request Review Agent

An automated GitHub pull request reviewer being built incrementally with FastAPI,
LangGraph, specialized AI agents, PostgreSQL/PGVector, and Redis. The goal is a small,
understandable portfolio project with a working demo and defensible engineering choices.

**Current implementation: Phase 4 — Security Agent.** A specialized security prompt,
provider abstraction, validated structured output, one output-repair attempt, and
deterministic mock mode now connect normalized PR context to a stored security-only
review. Signed webhooks, Redis acceptance, Alembic persistence, and read APIs remain
available. LangGraph, other reviewers, automatic queue processing, and indexing
arrive in later phases.
The original requirements are preserved in [docs/PRD.md](docs/PRD.md).

## Architecture

Implemented request flow:

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
for a quick webhook response. **There is no queue consumer yet:** accepted jobs
remain queued until the processing worker is added in Phase 8. The GitHub client
can already retrieve normalized diffs independently using the command below.

Phase 3 adds a separate persistence/read path: a caller saves a validated job and
PR context through `ReviewRepository`, completes it with findings in one explicit
transaction, and reads the result through FastAPI. The synthetic demo below
exercises this path. Webhook acceptance still only writes to Redis; accepted jobs
do not appear in PostgreSQL or the review API until an ingestion caller persists them.

The Phase 4 command is one such caller. It accepts normalized PR JSON, runs the
Security Agent through the configured provider, and can persist the outcome for
the existing read API. This is a manually invoked single-agent path; it does not
consume webhook jobs or publish to GitHub.

```mermaid
flowchart LR
    Context[Normalized PR JSON] --> Bound[Bound input / security prompt]
    Bound --> Provider[Mock or compatible LLM provider]
    Provider --> Validate[Pydantic + diff citation checks]
    Validate --> Result[Typed security result or recorded failure]
    Result --> Persist[Optional transaction: persist partial review]
    Persist --> Read[Existing review API]
```

Planned review flow, **not implemented yet**:

```mermaid
flowchart TD
    GitHub[GitHub PR webhook] --> API[FastAPI: verify signature / accept job]
    API --> Queue[Redis job queue and status]
    Queue --> Ingest[LangGraph: ingest PR]
    Ingest --> Context[Retrieve repository context]
    PGVector[(PostgreSQL + PGVector)] --> Context
    Context --> Security[Security agent]
    Context --> Testing[Testing agent]
    Context --> Quality[Quality agent]
    Context --> Documentation[Documentation agent]
    Security --> Aggregate[Deduplicate / deterministic risk scoring]
    Testing --> Aggregate
    Quality --> Aggregate
    Documentation --> Aggregate
    Aggregate --> Persist[Persist review]
    Persist --> PGVector
    Persist --> Publish[Publish GitHub comment]
```

## Project structure

```text
app/
  main.py                 # App factory and lifespan resource management
  api/routes/health.py    # Concurrent dependency readiness checks
  api/routes/webhooks.py  # Bounded body, signature validation, quick job acceptance
  api/routes/reviews.py   # Stored review, findings, and repository-history reads
  core/config.py          # Environment and .env configuration
  core/logging.py         # JSON application logs
  core/security.py        # HMAC-SHA256 signature verification
  core/exceptions.py      # Sanitized GitHub API failures
  db/session.py           # Async SQLAlchemy engine configuration
  db/models.py            # Four related tables with database constraints
  db/repositories.py      # Transaction-scoped review persistence and queries
  schemas/github.py       # GitHub payloads and normalized PullRequestContext
  schemas/jobs.py         # Queue contract and webhook responses
  schemas/review.py       # Findings, completion contracts, enums, API responses
  schemas/agents.py       # Security output and agent/run results
  agents/security_agent.py # Validation, one repair, evidence checks, bounded input
  prompts/security.py     # Security-only responsibilities and evidence instructions
  services/github_service.py # GitHub reads, pagination, retries, comment API
  services/job_queue.py   # Atomic delivery deduplication and enqueue
  services/llm_service.py # Provider protocol, compatible HTTP adapter, mock provider
  services/review_service.py # Single-agent persistence and deterministic risk
docker/postgres/init.sql  # Enable vector in a fresh database
examples/pull_request.opened.json # Sample webhook, with fictional commit SHAs
examples/security_pr.json # Synthetic normalized security-review fixture
scripts/send_webhook.py   # Signed local webhook demo
scripts/fetch_pr.py       # Read a real PR as normalized JSON
scripts/seed_demo.py      # Persist a labeled synthetic review without an LLM
scripts/review_security.py # Run the Security Agent, optionally persist its result
alembic/                  # Async migration environment and versioned schema
alembic.ini
tests/
  conftest.py             # Mocked database and Redis resources
  unit/                   # Config, health, signatures, schemas, GitHub client, queue
  integration/            # Signed webhook to Redis; queued job to mocked GitHub context
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
| `LLM_MAX_OUTPUT_TOKENS` | `2048` | Output token budget (128–8192) |
| `LLM_TOKEN_LIMIT_FIELD` | `max_completion_tokens` | Set `max_tokens` for providers using that field |
| `MAX_FILES_PER_REVIEW` | `30` | Maximum supplied textual diffs |
| `MAX_PATCH_CHARS` | `60000` | Total patch-character budget across those files |

Environment variables override `.env`. Compose supplies service hostnames `postgres`
and `redis`; host Python uses `localhost`. Only `EMBEDDING_MODEL` and
`MAX_CONTEXT_CHUNKS` remain reserved for Phase 7. The LLM configuration is now active
for the Security Agent command; it does not cause the webhook handler to call an
LLM. Keep secrets in an untracked `.env`; never commit real tokens.

## API endpoints

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/health` | 200 if PostgreSQL, vector extension, and Redis are available; otherwise 503 |
| POST | `/webhooks/github` | Verify a signed delivery, enqueue supported PR events, return 202 |
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

`repository_chunks` and vector retrieval arrive in Phase 7. The existing Compose
initialization still enables PGVector; Phase 3 only migrates relational review data.

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
intended for one caller per review ID; concurrent worker leases/recovery arrive later.

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
controlled deferred-retry failure for future worker scheduling. Permanent HTTP
errors, refusals, interrupted output, empty content, and invalid response envelopes
fail without repeated generation. API redirects are not followed. The agent can
request one additional completion for invalid output, so the worst-case default
budget is **two completions × three transport attempts = six HTTP requests**.

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
model's advisory score does not choose the stored risk or recommendation. This is
not the future multi-agent aggregator; deduplication and all-agent scoring come later.

Ingestion commits before model I/O. The agent runs with no open database
transaction, and completion/findings or failure persist in a separate transaction.
Raw agent evidence stays in the returned agent result; the database stores the
existing common finding fields. A hard process crash can still leave `PROCESSING`
until the future worker implements recovery.

## Phase 2 webhook demo

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
survive metadata expiry. There is no consumer or automatic queue draining yet.

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
method requires write permission and is implemented but not called automatically.

`PullRequestContext` includes repository, PR number/title/description/author,
base/head branch and SHA, fork repository, changed files, patches, additions,
deletions, reported file totals, and coverage warnings. The client pages through
file results sequentially, up to GitHub's 3,000-file API limit, and preserves absent
patches as `null`. It marks context incomplete when reported file counts exceed
retrieved files. It checks the base/head SHAs before and after fetching files;
future ingestion can supply a queued job's expected SHAs to reject a stale revision.

Temporary GET failures retry at most three total attempts, with 0.5/1-second
backoff. Permanent errors, rate limits, and server `Retry-After` responses fail
without early retries; scheduling later attempts belongs to the future worker.
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

Tests cover configuration, health, signatures, payload validation, atomic enqueue,
delivery deduplication, metadata expiry, concurrent redelivery, queue outages and
deadlines, GitHub pagination, retries, rate limits, absent patches, file safety,
forks, and stale revisions. `httpx.MockTransport` mocks GitHub; `fakeredis[lua]`
executes the actual enqueue script against in-memory Redis. Database health I/O
is mocked. No GitHub credentials, real external writes, or LLM credits are required.
These tests do not claim to validate a real Docker deployment.

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

Phase 4 local verification on Python 3.11.9: **209 passed, 1 skipped** without a
database server; **243 passed, 1 skipped** with PostgreSQL 18 enabled. The skip is
the SQLite version of a concurrency case that passes on PostgreSQL. Ruff lint,
format checks, and dependency compatibility checks passed.
A real Uvicorn process served `/docs` and `/openapi.json` with HTTP 200 and
returned the expected HTTP 503 when connected to unavailable PostgreSQL/Redis
addresses. A Phase 2 live HTTP check also ran the signed sender against Uvicorn
and a TCP in-memory Redis simulator, exercising the actual enqueue script:
HTTP 202 on acceptance, one queue entry and the same review ID on redelivery,
and HTTP 403 for a tampered signature. GitHub calls were mocked during verification.
Phase 3 also verified `alembic upgrade head`, `alembic check`, repeated seeding,
and real HTTP review/findings/history reads against a separate temporary local
PostgreSQL cluster. The resolved Starlette version emits an upstream deprecation warning
about its HTTPX test-client support; the tests pass. Docker is not installed on
the implementation machine, so container builds and a healthy response against
real services remain unverified.

Phase 4 adds mock/no-network tests, provider wire-format and retry tests, structured
schema/repair/evidence checks, citation rejection, input budgets, deterministic
security scoring, and model-to-database integration. Provider HTTP is mocked with
`httpx.MockTransport`; no paid LLM requests run in automated tests. A live CLI and
HTTP check persisted a mock Security Agent result in PostgreSQL, verified it through
the review API, and confirmed repeated IDs reused the stored result. A standalone
mock CLI check also passed with database/Redis deliberately unavailable. Real LLM
account/model compatibility remains unverified; no paid LLM calls were made.

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
- **PostgreSQL with PGVector:** one database for future relational review data and
  contextual vector retrieval. Only the extension is initialized in Phase 1.
- **Redis:** a small script gives atomic job acceptance and delivery deduplication.
  Queue entries contain the whole job, so expiring status metadata cannot erase
  pending work. A consumer with acknowledgement/recovery and caching comes later.
- **GitHub boundary:** normalized Pydantic objects keep external payload details
  out of future agents. SHA checks detect stale ingestion; bounded retries and
  trusted URL construction keep failures controlled.
- **Explicit readiness:** checking the vector extension catches incomplete database
  setup. Health failures do not prevent access to the API documentation.
- **Small dependency surface:** LangGraph, LLM integrations, and Alembic arrive in
  the phases that use them. The compatible provider reuses HTTPX; it needs no model
  SDK or LangChain wrapper yet. There are no placeholder reviewer implementations.
- **Probabilistic/deterministic boundary:** model output is validated against a
  schema and the supplied diff; application code determines persisted security risk.
  Security-only output is partial so missing reviewer categories remain visible.

## Development plan

1. **Foundation (implemented):** FastAPI, settings, PostgreSQL/PGVector, Redis,
   Docker Compose, health endpoint.
2. **GitHub (implemented):** signature verification, webhook payload validation, GitHub client,
   normalized PR diff retrieval.
3. **Database (implemented):** repositories, pull requests, reviews, findings, Alembic migrations,
   transactional repository methods, and stored-review read APIs.
4. **Single agent (implemented):** security prompt, provider abstraction, validated output,
   one repair, deterministic mock mode, input limits, and optional persistence.
5. **LangGraph:** typed shared state, graph nodes, checkpointing.
6. **Multiple agents:** quality, testing, documentation; concurrent execution and partial results.
7. **PGVector:** repository indexing, embeddings, similarity retrieval, bounded context.
8. **Redis processing:** queue, worker, status tracking, temporary cache.
9. **GitHub output:** a single readable Markdown review comment.
10. **Workflow tests:** webhook-to-review integration with mocked external services.
11. **Polish:** retries, large-PR limits, timings, simulated load script, demo documentation.

## Limitations and future improvements

Phase 4 runs a manually invoked Security Agent, optionally persisting its partial
result. It does not process pending webhook jobs automatically. Until the worker
arrives, webhook deliveries accumulate in Redis and require a later ingestion caller
to create their database records. Redis status and PostgreSQL status are not yet
synchronized by a worker.
Basic input budgets are implemented; chunked review and advanced large-PR handling
remain future work. Infrastructure
containers must be verified on a Docker-capable machine. The current credentials
and configuration target local development. Dependencies use compatible version
ranges; release deployments should use a pinned dependency resolution.

As the review pipeline is implemented, its documentation will cover LangGraph
state transitions, agent responsibilities, deterministic
risk scoring, example output, and benchmark methodology. LLM reviews can produce
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
