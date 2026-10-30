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

## Phase 6: concurrent specialized reviewers

Implemented:

- Added Quality, Testing, and Documentation Agents with separate role prompts,
  category-specific Pydantic findings/reviews/results, and concrete suggestions.
  Testing/docs gaps are qualified against supplied context.
- Extracted shared input budgets, evidence checks, structured validation, safe
  errors, and one-repair behavior into `app/agents/base.py`; the Security Agent and
  its existing public helpers remain compatible.
- Default graph fans out to all four reviewers and waits for every branch before
  aggregation. Branches merge disjoint category keys in shared `agent_results`;
  errors/stage events remain additive. Reports include validated per-agent outcomes.
- Independent reviewer deadlines (`AGENT_TIMEOUT_SECONDS=210`) and safe failed
  outcomes isolate provider failures, exhausted repairs, timeout, invalid returns,
  and unexpected exceptions. Cancellation propagates for checkpoint recovery.
- Aggregation preserves successful findings, deduplicates across reviewers using
  Unicode-safe title normalization, sorts deterministically, and calculates weighted
  risk independently of model advisory scores. Full success/coverage is COMPLETED;
  failed/omitted categories or limited coverage is PARTIAL; all-failed is FAILED.
  Only complete zero-finding reviews can APPROVE.
- Schema version 2 records the full reviewer set. `--security-only` retains version
  1 topology and old checkpoints; incompatible resume is rejected. No business
  migration was necessary; Alembic remains at 0001 and checkpoints retain their
  separate `review_workflow` schema.
- Default CLI and `examples/multi_agent_pr.json` demonstrate all four reviewers
  without infrastructure or credits. Mock results are predefined fixture responses.
- Updated README and workflow guide with responsibilities, status semantics,
  deadlines, bounded request counts, pause/resume, and legacy compatibility.

Verified on Python 3.11.9:

- Default suite: 272 passed, 3 skipped. With PostgreSQL 18: 316 passed, 3 skipped.
  Skips are SQLite variants of concurrency and two durable-checkpointer tests;
  all PostgreSQL counterparts pass.
- Async barriers prove all four provider calls start before any completes. Tests
  cover each reviewer failing, all failing, invalid return data, timeout, output
  repair, distinct roles/schemas, coverage, recommendation rules, and deduplication.
- Cancellation tests wait for actual saved sibling outputs before interruption.
  A real PostgreSQL saver closes/reopens its connection and rebuilds the graph;
  only the unfinished reviewer runs again, preserving the other three outputs.
- Persistence/API integration covers COMPLETED/PARTIAL/FAILED and replay after
  final commit before checkpoint, without duplicate findings or repeated reviewers.
- Separate CLI processes paused/resumed/reused review
  `00000000-0000-4000-8000-000000000006`; it returned COMPLETED, four findings,
  score 7.7, and REQUEST_CHANGES. Legacy Phase 5 review ending 005 also resumed
  successfully with `--security-only`.
- Live Uvicorn HTTP review, findings, and repository-history reads returned 200
  and the four persisted categories. The temporary API process was stopped.
- `alembic check`: no new upgrade operations. Ruff lint/format: passed (82 files).
  Dependency compatibility: 71 installed packages compatible. CLI help checked.

Verification uses the owned temporary PostgreSQL cluster under `.venv`; the
existing Windows PostgreSQL service is never used. The temporary cluster is
stopped after checks. Docker builds and real provider accounts remain unverified;
no paid LLM calls or GitHub writes occurred.

This remains a manual workflow with one caller per review thread. Checkpoints
reuse completed work; unfinished nodes can run again. No semantic retrieval,
Redis worker/leases, status synchronization, or GitHub publishing is added here.

## Phase 7: revision-scoped PGVector repository context

Implemented:

- `services/embedding_service.py`: injectable embedding protocol, compatible HTTP
  adapter, explicitly configured model/endpoint/dimensions, separate credentials,
  strict response count/index/vector checks, normalization, bounded transport
  retries, and per-attempt deadlines. Real mode never silently becomes mock mode.
  Free mock mode uses deterministic lexical feature hashing, not semantic models.
- `schemas/context.py`: index requests/reports and bounded source snippets with
  paths, original line ranges, exact repository/revision, and similarity metadata.
- GitHub commit/tree methods resolve a branch/tag once, verify immutable SHA
  requests, traverse its pinned tree, and fetch selected content at the resolved
  commit. No payload URLs, symlinks, submodules, or submitted code are executed.
- `services/indexing_service.py`: common source/docs selection, dependency/build/
  generated/binary/oversized exclusions, line-anchored size chunks, byte/file/chunk
  budgets, batched embeddings, and whole-operation deadline. Incomplete trees and
  exhausted budgets remain explicit index coverage metadata.
- Alembic `0002`: `repository_indexes` and `repository_chunks`, variable-dimension
  PGVector column, revision/profile uniqueness, chunk ownership index, and cascading
  cleanup. Extension setup remains shared; downgrade preserves the vector extension.
  Added the `pgvector` Python dependency; no embedding SDK was necessary.
- `db/context_repository.py`: atomic snapshot upsert/row lock and chunk replacement,
  removal of stale chunks, repository/revision/profile-scoped cosine search, stable
  ranking, threshold, and top-k. No network operation spans a DB transaction.
- `services/context_service.py`: bound query additions, exact target base SHA only,
  exclude changed/renamed-from paths, and cap snippets/total characters. A short
  shared row lock keeps current coverage metadata and snippets consistent during
  a concurrent reindex, after embedding I/O completes.
- `POST /repositories/index`: bounded synchronous indexing with safe HTTP errors,
  counts, resolved commit/profile, mock flag, timestamps, and coverage warnings.
- Workflow context node optionally injects identical snippets into all reviewers.
  Missing/limited indexes, provider/storage failure, or timeout retain diff analysis
  and produce PARTIAL coverage. Disabled retrieval and valid no-match queries are
  normal. Findings remain anchored in changed-file evidence.
- Retrieval-enabled checkpoint version 3 validates the embedding/retrieval profile
  hash and saves snippet JSON, without vectors/clients/secrets. Diff-only versions
  1/2 retain compatibility. Saved context is reused after interruption.
- New `scripts/index_repository.py`, normalized source fixture, workflow flags
  `--with-context`/`--diff-only`, active environment/Compose settings, README updates,
  and `docs/CONTEXT.md`. `--mock` forces free embeddings and reviewers in the workflow.
- Test migrations pin version tables/reflection/inspection to each unique test
  schema while exposing public vector types. This prevents public application data
  from being mistaken for test migrations. Existing historical tests still pass.

Verified on Python 3.11.9:

- Default suite: 361 passed, 9 skipped. PostgreSQL/PGVector suite: 419 passed,
  9 skipped. All nine skips are SQLite variants of tests requiring PostgreSQL
  locks, durable checkpoints, or PGVector; PostgreSQL counterparts pass.
- Actual PostgreSQL 16.15 / PGVector 0.8.6 cosine ranking, exact-match threshold,
  top-k/character bounds, repository/base/profile isolation, and changed-path
  exclusion passed. No Python substitute for vector search is claimed.
- Atomic same-snapshot replacement, deleted-chunk cleanup, empty snapshots,
  embedding and SQL-write failures, index/retrieval deadlines, concurrent writers,
  and coverage changes during a concurrent reindex passed.
- Provider wire-format, response order/index/count/dimensions, nonfinite/zero/
  overflowing values, input budgets, retry/deferred-delay boundaries, and safe
  malformed-response/permanent-failure handling passed with mocked HTTP.
- A durable PostgreSQL checkpoint closed/reopened its connection and rebuilt the
  graph after index removal; resume reused saved snippets without model/embedding
  calls. Incompatible checkpoint mode/profile and out-of-diff findings are rejected.
- Live Uvicorn `POST /repositories/index` against a loopback GitHub fixture returned
  HTTP 200 and three stored chunks. Separate CLI processes paused/resumed/reused
  review `00000000-0000-4000-8000-000000000008` with saved snippets from tests,
  database code, and README. Review/findings/history GETs returned HTTP 200,
  COMPLETED, four findings, score 7.7, REQUEST_CHANGES.
- Alembic upgrade, downgrade/upgrade in isolated test schemas, and final
  `alembic check` passed with no schema drift. Ruff lint/format passed (98 files),
  CLI help checked, all 21 context-related settings matched Compose/example
  defaults, duplicate reserved settings removed, and 72 packages were compatible.

Verification used an owned workspace-local PostgreSQL/PGVector runtime and cluster
under ignored `.venv`; it never used or modified the existing Windows PostgreSQL
service. Temporary API/source servers and the database are stopped after checks.
Docker builds and real provider/model accounts remain unverified. GitHub/provider
network responses were mocked; no paid API calls or GitHub writes occurred.

Retrieval uses exact cosine search over bounded snapshots, without HNSW/IVFFlat,
semantic reranking, compiler-aware parsing, automatic reindexing, or pruning.
Normalized fixture indexing trusts its caller's revision provenance. Manual review
invocation and one caller per checkpoint thread remain required; queued jobs are
not consumed yet. Semantic relevance does not establish correctness or call graphs.

## Phase 8: Redis background processing and recovery

Implemented:

- `services/worker_queue.py`: validated bounded envelopes and atomic compare/claim
  around the existing pending list; persistent token-owned processing receipts,
  per-review leases, heartbeats, deferred retry structures, bounded maintenance,
  expiry recovery, and terminal status/ACK. Full payloads and active attempt counts
  survive expiring job/status display metadata. Script keys are explicit, with
  type checks before mutation; deployment targets standalone Redis.
- Token/envelope ownership checks prevent expired owners from renewing, deferring,
  or acknowledging another receipt. Busy review heads rotate; stale lease scores
  cannot reclaim a live owner. Malformed/binary envelopes are quarantined by digest,
  including corruption in durable processing/retry structures. Dead letters are
  bounded; valid failure records retain the job and a safe fixed error.
- `worker/locks.py`: nonblocking PostgreSQL session advisory lock on a dedicated
  autocommit connection. An old DB session still holding a review prevents a new
  lease owner from running it concurrently. Contention does not spend attempts;
  cancellation/cleanup releases or invalidates the connection.
- `worker/processor.py`: production graph/resource wiring, strict job/checkpoint
  identity checks, durable start/resume, canonical PostgreSQL terminal reuse,
  failure persistence after ingestion, and ending aborted graph checkpoints.
  Jobs failing before validated PR ingestion have Redis/dead-letter outcomes without
  invented PostgreSQL PR/review rows.
- `worker/runner.py`: independent heartbeat supervision, cancellation after lost
  ownership, bounded execution retries/backoff, whole-workflow deadlines, safe
  failures, retry exhaustion, partial/all-agent outcome preservation, and graceful
  stop windows. Redis ACK occurs after final result persistence.
- GitHub rate/deferred errors carry supported numeric Retry-After and primary
  reset windows. Workers schedule them without sleeping while owning a receipt;
  repeated rate failures back off and windows beyond seven days fail safely.
- `services/pr_cache.py`: temporary normalized PR data scoped by API origin,
  credential fingerprint, repository, PR number, and exact base/head SHAs. Cache
  hits verify live SHAs and refresh PR metadata; corrupt/mismatched/oversized/
  unavailable entries fall back to GitHub. Cached file data avoids repeat reads.
  The graph accepts a structural PR-source protocol for cached/direct ingestion.
- `GET /reviews/{id}/status`: queued/processing/terminal states, attempt counts,
  safe error, and optional UTC next retry. Reads work before DB ingestion and
  report safe Redis failures/expired status. Duplicate webhook responses retain
  the current job status instead of always claiming QUEUED.
- Resume recognizes an already committed FAILED review before restarting pending
  model branches, covering failure-commit/checkpoint gaps. Worker canonical-result
  checks cover successful final-commit/checkpoint/ACK gaps without duplicate findings.
- `python -m app.worker` CLI, `--once`, `--mock`, retrieval mode flags, Windows
  selector loop, structured logs, graceful signals, shared Compose configuration,
  and a separate scalable worker service after healthy API/migration startup.
- Eleven validated worker/cache settings, README and workflow/context guide
  updates, and `docs/WORKER.md` with operation, ownership, retry, cache, status,
  durability, and verification details. No new dependency or business migration;
  Alembic remains at 0002 and graph checkpoint versions remain 1/2/3.

Verified on Python 3.11.9:

- Default suite: 413 passed, 12 skipped. With PostgreSQL/PGVector and Redis:
  481 passed, 11 skipped. Service-enabled skips are SQLite variants of operations
  requiring PostgreSQL locks/checkpoints/vector support; all PG counterparts pass.
  The default run also skips the actual Redis script test.
- Queue tests cover concurrent claims, active duplicates, stale-token rejection,
  live-heartbeat protection, payload survival after metadata expiry, one-time retry
  promotion, malformed/binary/corrupt envelope quarantine, and key-type safety.
  Real Redis tests use isolated test key namespaces and never flush a database.
- Worker tests cover commit/ACK reuse, safe retry exhaustion, stale PR failure,
  contention, lost heartbeats, shutdown/cancellation, deadlines, rate-window
  scheduling, and partial/all-agent outcome synchronization.
- Actual PostgreSQL advisory locking blocked another owner and released after
  cancellation. An actual durable checkpoint was reopened by a replacement worker
  without repeating the four already checkpointed model calls.
- Live separate API/worker processes consumed signed webhook deliveries using
  PostgreSQL/PGVector, Redis 7.4.11 (Windows port), and loopback GitHub/model fixtures.
  `/health` returned 200/healthy with all three dependencies up. Status progressed
  QUEUED → PROCESSING → COMPLETED; review/findings reads returned four findings and
  risk 7.7. Redelivery reused the completed ID/status without another queue entry.
- Killed a worker with Security/Testing/Documentation pending writes already saved
  and Quality still blocked. Restarted Redis from its real AOF data, waited for the
  owned lease to expire, and started another worker. Only Quality reran. Cache reuse
  limited the two deliveries to one PR-file fetch. Recovered status was COMPLETED,
  attempts=2, findings=4, score=7.7; no processing receipts/leases remained.
- CLI help and idle `--mock --once --diff-only` checked. All eleven worker/cache
  defaults match `.env.example` and Compose; API and worker share environment.
  `alembic check` reported no drift. Ruff lint/format and 72-package compatibility
  checks passed; formatting covers 113 files.

All runtime verification used owned local resources under ignored `.venv` and
loopback endpoints. The existing Windows PostgreSQL service was never modified.
Temporary API/source/model processes and Redis/PostgreSQL are stopped after checks.
The Redis Windows artifact digest was verified against its release metadata.
Container builds/Compose startup and real provider accounts remain unverified.
No paid API calls, GitHub writes, or production queue/database operations occurred.

This is at-least-once processing, not a distributed exactly-once/failover guarantee.
Manual callers must not concurrently run worker-owned review IDs. Unfinished model
work can repeat; cache data and display statuses expire while queue payloads remain.
Indexing stays synchronous. Automatic dead-letter redrive/pruning, Redis Cluster,
global provider quotas, throughput benchmarks, and GitHub publishing are not added.

## Next phase

Phase 9 publishes the final review as one readable Markdown comment on the PR,
with stale-revision checks and controlled retry/idempotency behavior.
