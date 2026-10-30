# Reproducible demo

The free demo runs all four actual reviewer implementations with deterministic
mock responses. It requires Python 3.11+ and the installed project; the simulated
load harness additionally needs the `[dev]` dependencies. No token, database
server, GitHub account, or model credits are required.

```powershell
.\.venv\Scripts\python.exe scripts/demo.py
```

Open `artifacts/demo/review.md` in a Markdown preview. Compare it with
`artifacts/demo/limited.md`. The first completes all four reviewer categories with
four supported fixture findings, score **7.7**, and `REQUEST_CHANGES`. The second
restricts review to one file: it reports **PARTIAL**, one finding, and score **4.5**.
Both comments explicitly identify themselves as mock demos. Neither is persisted
to an application database or posted to GitHub.

Generated artifacts:

| File | Purpose |
| --- | --- |
| `review.json` | Actual complete workflow report, reviewer durations, findings |
| `review.md` | Rendered complete comment preview |
| `limited.json` | Actual limited report and coverage warnings |
| `limited.md` | Rendered partial comment with incomplete-review notice |
| `events.jsonl` | Actual structured workflow and agent logs, correlated by review ID |

Checked-in outputs are in [examples/demo](../examples/demo/review.md). Regenerate
them with `--output-dir examples/demo`. Timing values and log timestamps will
change. The demo uses class-default settings and explicitly mock providers;
inherited application feature flags and credentials do not change its behavior.
Tracing is disabled by the CLI. All submitted repository contents are handled as
text and are never executed.

To inspect the limited review's stage timings in PowerShell:

```powershell
Get-Content artifacts/demo/events.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object review_id -eq '00000000-0000-4000-8000-000000000012' |
  Select-Object stage, agent, status, duration_ms, files_reviewed, files_omitted
```

## Stateful delivery demo

The free artifact demo does not run an HTTP server or exercise Redis ownership.
For signed acceptance, persistence, actual worker processing, and fixture comment
publication, run the repeatable end-to-end scenarios:

```powershell
.\.venv\Scripts\python.exe scripts/test_suite.py --layer e2e
```

To include actual PostgreSQL/PGVector, Redis, advisory locks, and durable
checkpoints, set the two development test URLs and add `--services`, as described
in [TESTING.md](TESTING.md). Remote GitHub and model responses remain fixtures;
the tests create no real GitHub comment. The cases also demonstrate repair,
rate-limit retry, lost-response reconciliation, and recovery after comment
creation but before queue acknowledgement.

For an actual local deployment, follow the README's Compose/native instructions,
use the signed sender, and inspect the review/status/comment endpoints. Mock mode
simulates the reviewers; it still fetches GitHub for an actual queued PR unless
using the fixture harness. Publication remains explicitly opt-in. See
[PUBLISHING.md](PUBLISHING.md) before enabling writes.

## Simulated load

```powershell
.\.venv\Scripts\python.exe scripts/simulate_load.py --requests 100 --concurrency 20 --model-delay-ms 10 --output artifacts/load-100.json
.\.venv\Scripts\python.exe scripts/simulate_load.py --requests 1000 --concurrency 50 --model-delay-ms 10 --timeout-seconds 180 --output artifacts/load-1000.json
```

The harness runs signed FastAPI webhook requests through an in-process ASGI
transport. Acceptance and one background worker overlap. The production queue Lua,
review graph, four agents, aggregation, migrated SQLite persistence, PR context
cache, and saved-review HTTP reads run normally. GitHub uses an HTTP fixture,
Redis is `fakeredis`, checkpoints are in memory, ownership is a single-worker
substitute, and models return deterministic JSON after the configured simulated
delay. Publishing and retrieval are disabled. This harness uses temporary resources
and removes them on exit. It does not connect to the application's Redis/database.

Every tenth delivery repeats the preceding delivery ID by default. Unique
deliveries intentionally refer to the same fixture PR and commit, making cache
reuse measurable through file-fetch counts. This is a warm-cache scenario.
`--duplicate-every 0` disables duplicates. Each unique review still runs all four
reviewers; cache reuse never reuses an AI verdict.

The JSON result records inputs, boundaries, environment, UTC recording time,
HTTP acceptance latency percentiles, acceptance rate, observed pending-queue
high-water mark, review-read latency, total drain time, and completed reviews per
second. Acceptance excludes client semaphore waiting and measures the in-process
request/response. Initialization/migrations happen before measurement. Drain time
starts with request submission and ends when the worker has no more work after
submission finishes; saved-review reads are measured afterward. Queue depth is
sampled after acceptance and can miss transient peaks. An unsuccessful run exits
nonzero and records its errors; a deadline expiry records no successful benchmark.

Recorded results and commands are in
[examples/benchmarks/README.md](../examples/benchmarks/README.md). They demonstrate
queued acceptance followed by asynchronous draining on one development machine.
They do not measure network latency, PostgreSQL/Redis capacity, semantic review
accuracy, paid-provider latency, or production throughput. In particular,
`--concurrency 50` controls request submissions, not 50 concurrent LLM reviews.
The 1,000-request scenario is not a claim of 1,000 simultaneous expensive reviews.
