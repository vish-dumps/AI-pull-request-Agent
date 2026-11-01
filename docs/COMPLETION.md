# Project completion and acceptance

The [PRD](PRD.md) implementation is complete: all eleven phases and all twelve
final demonstration targets. Finishing work adds a dependency lock, hash-checked
Docker installation, CI, and one container demo covering PGVector and review delivery.

## Reproduce it

```powershell
python -m pip install uv==0.11.24
uv sync --locked --extra dev --extra build
uv run --no-sync python scripts/check_lock.py
uv run --no-sync python scripts/test_suite.py
```

For the full free demo, start Docker with Linux containers enabled, then run:

```powershell
uv run --no-sync python scripts/compose_smoke.py --keep
```

The result prints a live `swagger_url`, `review_id`, retrieved context, and exact
cleanup command. Open Swagger and read the review, findings, status, comment,
and publication endpoints with that ID. The repository index and example review
are already created. Omit `--keep` for an automated check with immediate cleanup.
Initial setup downloads public packages/images; no GitHub account or model credits
are required. `scripts/demo.py` also generates previews without Docker.

## PRD final-target acceptance

| Final demonstration target | Working evidence |
| --- | --- |
| 1. Start infrastructure using Compose | Actual Dockerfile, PostgreSQL/PGVector, Redis, API, and worker containers |
| 2. Open Swagger | `/docs` and `/openapi.json` return 200; `--keep` prints an interactive URL |
| 3. Send simulated PR webhook | Real HMAC-signed HTTP delivery; invalid signature rejected |
| 4. Show created job | HTTP 202 with ID and Redis status |
| 5. Show LangGraph execution | Actual graph, PostgreSQL checkpoints, and correlated logs |
| 6. Show four agents | Checkpoint contains security, quality, testing, and documentation results |
| 7. Show PGVector retrieval | Index API fetches three base-commit files; checkpoint contains three PGVector chunks |
| 8. Show aggregated findings | Four mock findings, weighted score 7.7, REQUEST_CHANGES |
| 9. Show PostgreSQL persistence | Stored review/findings read through API |
| 10. Show Redis state | Terminal status and deduplication survive restart; readiness recovers |
| 11. Show GitHub-style review | One HTTP comment in synthetic GitHub fixture and matching Markdown preview |
| 12. Run tests | Locked unit/integration/end-to-end suites and CI configuration |

The [actual Docker result](../examples/deployment/compose-smoke.json) includes the
index, retrieved paths/lines/similarities, reviewer categories, review outcome,
runtime versions, and cleanup. Its URLs are historical; `--keep` creates live
ones. Models and embeddings are mock; vector search, database, Redis, checkpoints,
worker, and HTTP delivery are real. Mock embeddings are lexical, not semantic.

## Final verification

Verified on 1 November 2026:

- Windows Python 3.11.9 with locked dependencies: **490 passed, 12 expected skips**.
- Separate locked Python 3.12.13 environment: **490 passed, 12 expected skips**.
- Locked Python 3.11 against Docker PostgreSQL 17.11 / PGVector 0.8.7 and Redis:
  **615 passed, 11 expected SQLite capability skips**.
- Full Docker demo passed, including PGVector context, all four reviewers,
  persistence, fixture publication, deduplication, and restart/readiness recovery.
- Interactive `--keep` mode and its live Swagger HTTP endpoint were checked, then
  the owned resources were removed. Schema/dependency checks found no conflicts.
- Ruff, lock/export consistency, package compatibility, and actionlint 1.7.12
  workflow validation passed. The CI commands ran locally; a hosted Actions run
  is not claimed.

## Dependency and CI maintenance

`uv.lock` pins dependencies; `requirements/docker.txt` is its hash-checked export
including the `build` extra for Hatchling. Docker installs with `--require-hashes`,
then builds the project without further dependency resolution/build isolation.
Python, PostgreSQL/PGVector, and Redis image digests are pinned.

After dependency changes:

```powershell
uv lock --default-index https://pypi.org/simple
uv export --frozen --extra build --no-dev --no-editable --no-emit-project --no-header --no-annotate --output-file requirements/docker.txt
uv sync --locked --extra dev --extra build
uv run --no-sync python scripts/check_lock.py
```

`.github/workflows/ci.yml` covers Windows/Linux and Python 3.11/3.12, service-backed
tests, and the Docker demo. Actions use commit SHAs, credentials are not persisted,
permissions are read-only, and no real provider secrets are needed. Docker evidence
is uploaded on success/failure. The workflow runs after changes are pushed; its
remote execution has not been observed here. Dependabot covers action/image updates.

## Scope of completion

No further implementation phase is needed to demonstrate the PRD. Real GitHub and
paid-provider validation needs a chosen account/provider and credentials.
Production auth, secrets, TLS, admission limits, failover, and capacity are separate
deployment decisions. Mock tests do not establish semantic accuracy or production
readiness. A browser screenshot could not be captured in this session; actual
structured evidence and reproducible demos are provided instead.
