# Post-PRD milestone: Docker/Compose validation

The original PRD ends at Phase 11. This follow-on milestone verifies the packaged
application in actual Linux containers rather than adding another review feature.

## Reproduce the isolated check

Install the project locally and run Docker with Linux containers enabled:

```powershell
.\.venv\Scripts\python.exe scripts/compose_smoke.py --prepare-only
.\.venv\Scripts\python.exe scripts/compose_smoke.py
```

The first command validates and writes configuration without starting containers.
The second builds the application Dockerfile and starts PostgreSQL/PGVector,
Redis, API, worker, and a synthetic GitHub fixture service. Initial image downloads
and dependency installation can take several minutes. Docker Desktop must already
be running when using the script yourself.

The harness rejects remote Docker endpoints and Windows-container engines before
starting resources. Unix sockets, local named pipes, and loopback TCP endpoints
are supported.

The harness renders the project's Compose file to canonical JSON, then creates a
standalone smoke configuration. This preserves the actual Dockerfile, startup
migration, health checks, database image, Redis AOF configuration, and worker
command. Canonical configuration also lets it replace published ports and resource
names directly, avoiding merged port lists. See Docker's
[configuration reference](https://docs.docker.com/reference/cli/docker/compose/config/).

Each run uses a random `prreview-smoke-<id>` project, uniquely named volumes and
network, and dynamic API/fixture ports bound to `127.0.0.1`. PostgreSQL and Redis
have no host port mappings, so existing host services on 5432/6379 are unaffected.
Existing containers and external volumes/networks are not reused. Application
settings and Compose overrides inherited from the shell are cleared before
rendering `.env.example`; the user's `.env` file is not loaded.

The API and worker use mock models, disable retrieval/tracing, and direct every
GitHub request to the synthetic fixture container. Fixture publication is enabled
explicitly, including mock-review publication, with a fixture-only token. This
exercises the real outgoing-comment implementation without touching an actual
GitHub repository. These flags are specific to the disposable test configuration;
normal Compose publishing remains off by default.

## Checks and evidence

The smoke run verifies:

1. Container build/startup, automatic Alembic migration, and healthy readiness for
   PostgreSQL, PGVector, and Redis.
2. A rejected invalid signature followed by HTTP 202 for a signed fixture delivery.
3. Actual Redis queue consumption and four-agent review in the separate worker
   container, persisted findings, score 7.7, and COMPLETED status.
4. One Markdown comment created in the fixture container, a matching frozen
   preview, and PUBLISHED status from the API.
5. Redelivery returns the same review ID and does not create another comment.
6. Worker restart preserves the completed review/publication.
7. Stopping Redis makes readiness return HTTP 503; starting it restores HTTP 200
   while the saved review remains readable.
8. Redis retains the terminal status and delivery deduplication across restart;
   `alembic check` reports no migration drift and container `pip check` reports no
   incompatible installed dependencies.

The project is stopped and its own disposable containers/network/volumes removed
in a `finally` block. Other projects, global Docker caches, and existing Windows
services are left alone. The built image/cache is retained for subsequent runs.
No global prune or database flush is performed.

Each run writes ignored evidence under `artifacts/prreview-smoke-<id>/`:

- `compose.json`: exact synthetic configuration, including a generated local
  webhook secret; no user token or paid-provider key.
- `commands.log`: Compose command output, including build/startup diagnostics.
- `command-*.stdout.log` / `command-*.stderr.log`: live UTF-8 output while each
  command runs, including a long first build on Windows.
- `containers.log`: collected application/infrastructure logs before cleanup.
- `result.json`: assertions completed and cleanup outcome.

If startup or an assertion fails, the command exits nonzero and cleanup still
runs. Use the logs to diagnose the failure. Functional fixture/isolation tests
also run in the normal service-free pytest suite.

## Recorded validation

The [saved smoke result](../examples/deployment/compose-smoke.json) records a
successful local run on Docker Engine 29.9.2 / Compose 5.6.1 with Linux containers.
It ran Python 3.11.18, PostgreSQL 17.11, and PGVector 0.8.7. The result includes the
resolved application-library versions, four findings, score 7.7, exactly one
fixture comment, restored readiness, no schema drift, no dependency conflicts, and
successful resource cleanup. Project-labeled container/volume/network listings
were empty afterward.

The first attempt found a Windows default-codepage failure when capturing Docker's
Unicode build output. Log capture now uses explicit UTF-8 files; the corrected
run completed all checks. Both attempts removed their own disposable resources.
The host suite additionally passed **489 tests with 12 expected skips**, and Ruff
lint/format plus compatibility checks passed. Native-service results from Phase 11
remain recorded separately; the Docker smoke run uses its own actual containers.

This check validates a local Docker deployment. Real GitHub account/token behavior,
paid-provider responses, production authentication/secrets, TLS ingress, failover,
release dependency locking, and production capacity remain separate work. The
container's compatible dependency resolution may differ from the host virtual
environment; a passing smoke run does not establish exhaustive version coverage.
