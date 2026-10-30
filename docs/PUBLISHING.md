# Phase 9: GitHub review comments

The publisher posts one readable Markdown issue comment for a saved review. It
groups Security, Testing, Code Quality, and Documentation findings, displays the
stored risk/recommendation and reviewed commit, and preserves partial-review
coverage warnings. Recommendations appear as text within the comment.

Publishing is separate from analysis. GitHub failure leaves the stored review
intact and has its own durable status, retry budget, and reconciliation record.
The worker performs this step after persistence and before acknowledging the job.
The graph's existing checkpoint versions remain compatible.

## Preview without posting

Render the four-reviewer fixture without infrastructure, credentials, or paid calls:

```powershell
.\.venv\Scripts\python.exe scripts/publish_review.py --demo
```

See [the generated example](../examples/review_comment.md). It is explicitly
labeled as predefined mock output. The demo never constructs a publishing client.

Preview a saved review using its UUID:

```powershell
.\.venv\Scripts\python.exe scripts/publish_review.py REVIEW_UUID
Invoke-WebRequest http://localhost:8000/reviews/REVIEW_UUID/comment
Invoke-RestMethod http://localhost:8000/reviews/REVIEW_UUID/publication
```

The comment endpoint returns UTF-8 `text/markdown`. Both read endpoints perform
only database reads. If a publication body is already frozen, previews show that
exact body. Otherwise the preview uses current renderer limits. Unknown reviews
return 404; unfinished/failed analysis has no normal preview and returns 409.

The renderer uses the saved public review fields, not checkpoints, prompts,
retrieved source, embeddings, raw evidence payloads, or model advisory scores.
It escapes untrusted Markdown/HTML, flattens injected headings/control characters,
and neutralizes mentions. Findings retain filenames, reliable added-line anchors,
severity, descriptions, and actionable suggestions. Size limits omit whole
findings explicitly, prioritizing severity/confidence; omitted sections do not
claim to be clean. Empty partial sections report only the available results.

## Enable publishing

Configure an authenticated token and opt in through the untracked `.env`:

```dotenv
GITHUB_TOKEN=YOUR_TOKEN
GITHUB_PUBLISH_ENABLED=true
GITHUB_PUBLISH_MOCK_REVIEWS=false
```

Use a fine-grained PAT or user/bot token that supports `GET /user`, with Pull
requests read/write access to the intended repository. Optional repository indexing
also needs Contents read access. This phase verifies comment authorship through
the authenticated numeric user ID. GitHub App installation tokens that do not
support `/user` are not supported by this publisher.

Start/restart the worker after updating settings:

```powershell
.\.venv\Scripts\python.exe -m app.worker
```

In Compose, recreate API/worker configuration with `docker compose up --build -d`.
The API and worker share the environment. There is no public HTTP endpoint that
posts an arbitrary comment. To publish a specific saved review explicitly:

```powershell
.\.venv\Scripts\python.exe scripts/publish_review.py REVIEW_UUID --publish
```

This CLI still requires `GITHUB_PUBLISH_ENABLED=true` and a token. It acquires the
same PostgreSQL review lock as the worker. Avoid concurrently operating manual
review/resume commands on that review. The preview command omits `--publish`.

Mock reviews and older reviews whose execution mode is unknown are skipped by
default. New graph ingestion persists the real/mock mode; migration `0003` leaves
old modes null rather than inventing provenance. `GITHUB_PUBLISH_MOCK_REVIEWS=true`
explicitly permits demos/unknown legacy modes; use that only for deliberate demo
publication. Known mock output remains labeled in the comment. Failed analysis
is never published as a successful clean review.

## Revision and duplicate protection

For a new write, the publisher verifies the PR is open, unmerged, targets the
stored repository, and matches both the reviewed base and head SHAs. It repeats
the live check immediately before recording the outgoing write. Closed, moved,
merged, or unverifiable PRs are skipped. The body shows the reviewed head SHA.
Issue-comment creation cannot atomically bind a POST to a commit; a PR can still
change in the small interval after the last check.

Each saved review has one `review_publications` row and a frozen Markdown body,
including a hidden marker containing its UUID/head SHA. A bounded scan recognizes
only an exact body authored by the authenticated numeric user ID. Copied markers
from other authors do not count. Pagination is constructed locally; links returned
by GitHub only indicate whether another page exists. An incomplete scan never
proves absence. Endpoint changes or author changes cannot redirect a pending write.

This produces one comment per saved review. New deliveries/review IDs can produce
their own comments. The publisher does not overwrite or delete other comments.
A recorded PUBLISHED result remains final if someone later edits/deletes that
comment; the worker does not automatically recreate it.

## Status, retry, and uncertainty

`GET /reviews/{id}/publication` exposes attempts, POST attempts, comment ID, safe
error/reason, retry time, and publication time independently of analysis status.

| Status | Meaning |
| --- | --- |
| `DISABLED` / `NOT_REQUESTED` | No publication row; feature disabled or not yet requested |
| `PENDING` | Safe work remains; a supported read/rejection failure can schedule a retry |
| `SENDING` | Outgoing-write intent was committed before the HTTP POST |
| `PUBLISHED` | Successful POST or verified reconciliation; comment ID saved |
| `SKIPPED` | Failed/mock/unknown-mode analysis, stale/closed PR, or unsuitable result |
| `FAILED` | Definitive/configuration failure, scan limit, or exhausted safe retry budget |
| `UNCERTAIN` | A previous write may have succeeded; no automatic repost |

`SENDING` commits before network I/O, without keeping a database transaction open.
Successful responses save PUBLISHED in a separate transaction. If that save or
the worker ACK fails, the next worker reuses the saved review, scans for the frozen
comment, and records its ID without rerunning reviewers or posting a duplicate.

Transport failures, server errors after POST, malformed successful responses, and
cancellation after SENDING can have unknown outcomes. A resumed SENDING/UNCERTAIN
attempt is read-only: it reconciles a matching comment or remains UNCERTAIN. This
also covers a crash after intent was committed but before the POST started. Such
a case can leave an unpublished comment requiring operator attention; the service
does not guess that another POST is safe.

After checking GitHub, rerun the explicit `--publish` command to reconcile an
uncertain attempt. It still does not repost if the comment is absent. If a new
analysis/publication is wanted after investigating the prior outcome, use a new
delivery/review ID. There is no force-repost switch. Terminal skipped/failed rows
also stay final when settings change; inspect their reason before creating a new
review.

Read failures and definitive rate-limit rejections have a separate bounded retry
budget. POSTs are never retried inline. A supported 403/429 rejection can return
to PENDING because no comment was created. Other permanent rejections fail; POST
server/transport errors remain uncertain. Rate windows/deferred read responses
are honored up to seven days; oversized requested windows fail safely.

The worker delays PENDING publication without spending another analysis attempt
or repeating model requests. Publication failure/uncertainty does not change a
completed/partial review to FAILED. Job status can be COMPLETED while publication
requires attention; inspect the publication endpoint for the current outcome.
Completed Redis display hints may remain until TTL expiry after manual reconciliation.

## Limits and storage

| Setting | Default | Bound / purpose |
| --- | --- | --- |
| `GITHUB_PUBLISH_ENABLED` | false | Opt in to outbound writes |
| `GITHUB_PUBLISH_MOCK_REVIEWS` | false | Explicitly permit mock/unknown legacy reviews |
| `GITHUB_PUBLISH_MAX_ATTEMPTS` | 3 | Safe automatic publication rounds, maximum 5 |
| `GITHUB_PUBLISH_RETRY_SECONDS` | 30 | Initial exponential retry delay |
| `GITHUB_PUBLISH_TIMEOUT_SECONDS` | 60 | Whole publication round, maximum 300 seconds |
| `GITHUB_COMMENT_MAX_BYTES` | 50000 | UTF-8 body budget, 2048–60000 |
| `GITHUB_COMMENT_MAX_FINDINGS` | 40 | Displayed findings, maximum 100 |
| `GITHUB_COMMENT_MAX_PAGES` | 10 | Bounded history scan, 100 comments/page |

A timeout before SENDING can retry; after SENDING it preserves uncertainty.
Deadline recovery has a further five-second DB-only window. Explicit read-only
reconciliation can occur after the safe automatic-round budget is exhausted;
it never increases the number of writes. A frozen body is reused across retries
even when formatting limits change.

Migration `0003` adds nullable `reviews.mock_mode` and the publication table with
a cascading one-to-one review key, outcome/count constraints, author/destination
binding, frozen body, retry metadata, and timestamps. Alembic owns these business
tables; LangGraph retains its separate library-managed schema. No new dependency
or graph topology/state version is needed. Preview and publishing reuse the same
deterministic renderer.

## Verification

Tests cover formatting, partial/no-findings wording, byte/finding caps, hostile
Markdown/mentions, actor/body matching, bounded pagination, revision changes,
closed PRs, mock/legacy suppression, safe retries, cancellation/deadlines,
post-response uncertainty, commit-before-record failure, and worker recovery
without repeated model calls. Storage/migrations run on SQLite and PostgreSQL.

A live loopback HTTP run used separate API/worker/CLI processes, PostgreSQL and
Redis, and GitHub/model fixture servers. It verified one POST per review, preview
and read endpoints, redelivery reuse, a created comment with a dropped response,
read-only reconciliation, and default mock suppression. No real GitHub comment
or paid model request was made. Docker and real token/account behavior remain
unverified. This is controlled side-effect recovery, not a distributed exactly-once
or commit-conditional GitHub write guarantee.

References: [GitHub issue comments](https://docs.github.com/en/rest/issues/comments),
[PR snapshots](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request),
[retry guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api).
