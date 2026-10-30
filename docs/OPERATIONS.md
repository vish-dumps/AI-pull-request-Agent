# Operating the review agent

Run migrations before starting the native API/worker. Compose already runs them
before the API and starts the worker after API readiness. See the README for
installation and [WORKER.md](WORKER.md) for receipt ownership details. The supplied
credentials and exposed ports are intended for local development.

## Trace a review

`LOG_LEVEL=INFO` emits JSON events with `review_id`, `repository`, `pr_number`,
`stage`, `status`, and stage/agent durations. Reviewer branches add `agent` and
`attempt`; worker execution carries correlation into GitHub/model retries and PR
cache events. Context variables isolate parallel reviewers and simultaneous
reviews. Context is reset on return, failure, and cancellation.

`context` emits `files_reviewed`, `files_omitted`, and `patch_chars`. PR cache
events expose `cache_hit`; count true/false events for an observed hit rate.
Reviewer JSON results also retain `duration_ms`, and workflow checkpoints retain
completed stages and reviewer results. There is no external metrics stack.
Durations are elapsed wall-clock times for that stage, not billable model time.

Examples of actual log output are in [events.jsonl](../examples/demo/events.jsonl).
The formatter includes an allowlist of structured fields. Application log messages
never deliberately include prompts, PR contents, tokens, provider response bodies,
or raw exception details. HTTPX/HTTPcore and SQL engine logs are restricted to
WARNING even when application DEBUG is enabled, avoiding their normal verbose
request/parameter dumps. This is not a general redactor for arbitrary log messages;
new logging code must keep sensitive data out of message strings and extras.

Useful read-only checks with Compose:

```powershell
Invoke-RestMethod http://localhost:8000/health
docker compose logs -f worker
docker compose exec redis redis-cli LLEN reviews:pending
docker compose exec redis redis-cli HLEN reviews:processing
docker compose exec redis redis-cli ZCARD reviews:delayed
docker compose exec redis redis-cli LLEN reviews:dead
```

Health is readiness for PostgreSQL, PGVector, and Redis. API documentation remains
available when readiness fails. A healthy API does not prove that a worker is
running; growing pending depth with no worker completion logs needs investigation.

## Retry and deadline policy

| Boundary | Default policy | Outcome after exhaustion |
| --- | --- | --- |
| GitHub reads | Up to 3 attempts for transport errors/5xx, exponential 0.5 s then 1 s waits; 15 s request timeout | Infrastructure retry by worker where appropriate; rate-limit delays are scheduled, not slept inline |
| Compatible model | Up to 3 attempts for transport/429/5xx; 30 s request timeout; bounded short Retry-After | That reviewer fails; other reviewers finish and coverage becomes partial |
| Output validation | One repair after invalid schema/evidence | That reviewer fails without exposing untrusted output in feedback/logs |
| Embeddings | Up to 3 attempts; 30 s request timeout; retrieval deadline 45 s | Diff-only fallback with visible coverage warning/partial outcome |
| Reviewer branch | 210 s deadline per reviewer | Failed branch; successful siblings preserved |
| Ingest/persist transactions | Graph retries transient DB failures twice in total | Worker can schedule infrastructure retry; deterministic/schema conflicts terminate |
| Worker execution | 600 s per execution; up to 3 analysis attempts, delayed 10 s then 20 s for ordinary infrastructure failure | Persist failure when possible and retain diagnostic dead-letter outcome |
| Receipt ownership | 30 s lease, heartbeat every 10 s | Expired receipt recovered; PostgreSQL advisory lock fences overlapping owners |
| Publication | 60 s attempt deadline; own 3-round budget; safe deferred retries start at 30 s, respecting GitHub rate limits | Independent FAILED/UNCERTAIN publication; successful analysis remains saved |

Model retry waits accept only bounded Retry-After values; long/invalid deferrals
become a failed reviewer rather than holding the worker indefinitely. A rate-limit
delay from GitHub can exceed the short retry interval; worker/publication scheduling
honors it within the supported scheduling window. See configuration for exact caps.
Transport retries, schema repair, graph transaction retries, worker attempts, and
publication attempts are distinct budgets. Publication retries never increment
analysis execution attempts or rerun completed reviewers.

GitHub comment POST is never retried inline after an ambiguous transport error,
5xx, malformed success, or crash around the write. The outgoing SENDING intent
and frozen body are durable first. Recovery searches for an exact body under the
authenticated author. It does not blindly send a second comment. A definitive
rate-limit rejection can be deferred safely. See [PUBLISHING.md](PUBLISHING.md).

## Large PRs and incomplete coverage

Defaults are `MAX_FILES_PER_REVIEW=30`, `MAX_PATCH_CHARS=60000` per reviewer,
`MAX_CONTEXT_CHUNKS=3`, and `MAX_CONTEXT_CHARS=6000` for retrieved base-commit
snippets. `AGENT_TIMEOUT_SECONDS` bounds each reviewer independently.

When input exceeds a file or patch budget, selection prioritizes security,
authentication, permissions, payment, database/migration, and configuration paths,
then other source, tests, docs, and generated/vendor/lock artifacts. Original
ordering breaks ties. This is a filename heuristic, not semantic risk scoring.
Within an oversized patch, changed-line windows keep up to two neighboring context
lines, prefer additions, and preserve real unified-diff line positions. Windows
that do not fit are omitted; source lines are never cut into invented fragments.
Smaller later files can still be reviewed when an oversized line/window is skipped.
Patches that already fit retain their original form.

Every omission/truncation yields coverage warnings. They survive the context
checkpoint, reach all agents, make aggregation PARTIAL, and appear in the Markdown
incomplete-review notice. A partial review with no findings recommends COMMENT.
Supported high-risk findings can still recommend REQUEST_CHANGES. Binary/missing
patches and unavailable repository context also produce incomplete coverage.

Limits bound agent input, not total upstream download cost. GitHub file retrieval
still inspects up to its existing 3,000-file API ceiling before selecting review
input, and original normalized context can be checkpointed. Exhaustive chunked
review of every file, weighted per-category file policies, and application-level
queue admission/backpressure remain future improvements.

## Recovery checks

- **Status unavailable/expired:** `GET /reviews/{id}/status` is temporary Redis
  display state (default TTL 24 hours). Read `GET /reviews/{id}` for durable history.
  Redis outage returns 503 rather than inventing a status.
- **Worker restart/outage:** restore connectivity and restart the worker. It
  recovers expired receipts and resumes durable checkpoints. Do not delete receipt
  keys, flush Redis, or manually copy a receipt into the pending list.
- **PR moved while queued:** the old job fails its SHA check. A fresh signed
  synchronize delivery represents the new revision. It is a new review.
- **Partial/all-failed review:** inspect reviewer errors and coverage warnings;
  correct provider/configuration/data availability. A finished failed/partial job
  does not silently rerun indefinitely. Trigger a new review deliberately.
- **Publication attention:** inspect `/reviews/{id}/publication` and the read-only
  `/comment` preview. Use the documented explicit publishing CLI for reconciliation
  under the review lock. An UNCERTAIN result with no matching comment needs operator
  investigation; it never authorizes a force repost automatically.
- **Shutdown:** the worker stops accepting new receipts, permits bounded graceful
  completion, then leaves unfinished receipts recoverable. Compose allows 75 s
  before forced termination. Native Windows uses a Selector loop for Psycopg.

For service-backed verification, run `scripts/test_suite.py --services` with
explicit development URLs. The separate [Compose smoke check](DEPLOYMENT.md) has
verified local Linux containers, migrations, review delivery, fixture publishing,
and Redis readiness recovery. Real GitHub/provider accounts and production
configuration still require target-environment validation; local simulated load
results are not deployment certification.
