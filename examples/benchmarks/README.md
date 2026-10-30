# Recorded simulated load

These JSON files are actual single-run measurements from the Phase 11 harness on
Python 3.11.9 / Windows build 26200, recorded on 2026-10-30 UTC. They use in-process
ASGI HTTP, temporary migrated SQLite, `fakeredis` executing production Lua, one
worker, in-memory checkpoints, fixture GitHub responses, and deterministic mock
models with a configured 10 ms delay per request. Publishing and retrieval are off.
No network deployment or paid provider is measured.

| Measurement | [100 deliveries](mock-load-100.json) | [1,000 deliveries](mock-load-1000.json) |
| --- | ---: | ---: |
| Submission concurrency limit | 20 | 50 |
| HTTP 202 responses | 100 | 1,000 |
| Deduplicated redeliveries | 10 | 100 |
| Unique completed reviews | 90 | 900 |
| Errors / partial reviews | 0 / 0 | 0 / 0 |
| Mock model calls | 360 | 3,600 |
| Acceptance p95 | 1.640 ms | 1.455 ms |
| Acceptance phase | 0.1685 s | 1.4884 s |
| Observed max pending queue | 90 | 900 |
| Drain time from submission start | 9.005 s | 85.458 s |
| Completed reviews / second | 9.99 | 10.53 |
| Saved-review read p95 | 13.872 ms | 9.534 ms |
| Fixture file-list fetches | 1 | 1 |
| Pending / inflight after drain | 0 / 0 | 0 / 0 |

Commands used:

```powershell
.\.venv\Scripts\python.exe scripts/simulate_load.py --requests 100 --concurrency 20 --model-delay-ms 10 --output examples/benchmarks/mock-load-100.json
.\.venv\Scripts\python.exe scripts/simulate_load.py --requests 1000 --concurrency 50 --model-delay-ms 10 --timeout-seconds 180 --output examples/benchmarks/mock-load-1000.json
```

Each new delivery uses the same fixture PR/base/head; every tenth request repeats
the previous delivery ID. The first review fetches files and later reviews reuse
revision-scoped context after a fresh PR check. These warm-cache measurements do
not represent a workload of unrelated repositories or commits. Every unique review
still runs all four agents and persists independent findings.

There was no hardware normalization, baseline comparison, confidence interval,
production traffic, or 1,000 simultaneous reviews. Timings include local process
scheduling and SQLite costs; simulated delay does not reproduce provider behavior.
Queue peaks are sampled. Initialization is outside measured acceptance/draining,
and readback happens after draining. Re-running may produce different results.
See [DEMO.md](../../docs/DEMO.md#simulated-load) for the full methodology and limits.
