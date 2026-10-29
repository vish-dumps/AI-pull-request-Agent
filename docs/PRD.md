# Build: AI Pull Request Review Agent

You are a senior AI/backend engineer helping me build a portfolio-quality project called:

# AI Pull Request Review Agent

The goal is to build a working, technically impressive automated code-review system that analyzes GitHub pull requests using multiple specialized AI agents.

This project is intended for software engineering / AI engineering interviews.

It should demonstrate:

- Multi-agent AI orchestration
- LangGraph workflows
- LLM structured outputs
- FastAPI backend design
- Async processing
- GitHub webhook integration
- PostgreSQL persistence
- PGVector-based contextual retrieval
- Redis caching / job state
- Parallel agent execution
- Retry and failure handling
- Practical software architecture
- Dockerized local deployment

However:

DO NOT over-engineer this into an enterprise platform.

The project should be realistic enough to discuss confidently in an interview, but small enough that one developer can understand and maintain the entire codebase.

---

# 1. REQUIRED TECH STACK

Use the following technologies.

## Core

- Python 3.11+
- FastAPI
- LangGraph
- LangChain where useful
- Pydantic
- PostgreSQL
- PGVector
- Redis
- SQLAlchemy
- Alembic
- httpx
- GitHub REST API
- Docker
- Docker Compose

For LLM integration, design a provider abstraction.

The implementation should allow an OpenAI-compatible API or another LLM provider to be plugged in through environment variables.

Do not tightly couple the application to one model.

Use asynchronous Python where it provides a genuine benefit.

---

# 2. HIGH-LEVEL PRODUCT

A GitHub repository owner should be able to configure a webhook pointing to this application.

When a pull request is opened, synchronized, or reopened:

GitHub
→ Webhook
→ FastAPI
→ PR ingestion
→ LangGraph workflow
→ Context retrieval
→ Parallel specialized reviewers
→ Aggregation
→ Final review
→ GitHub comment

The application analyzes the PR diff and produces a structured review.

Example final output:

## AI Pull Request Review

Risk Level: Medium

### Security

- Possible SQL injection in `users.py`
- User-controlled value appears to be concatenated into a query

### Code Quality

- Duplicate validation logic detected
- Consider extracting it into a helper function

### Testing

- New authentication failure branch has no test coverage

### Documentation

- New environment variable `JWT_SECRET` should be documented

### Summary

3 important issues detected.

Recommended action: REQUEST CHANGES

---

# 3. CORE ARCHITECTURE

Organize the application approximately like:

app/

    main.py

    api/
        routes/
            webhooks.py
            reviews.py
            health.py

    agents/
        security_agent.py
        quality_agent.py
        testing_agent.py
        documentation_agent.py

    graph/
        workflow.py
        state.py
        nodes.py

    services/
        github_service.py
        llm_service.py
        embedding_service.py
        review_service.py
        context_service.py

    db/
        models.py
        session.py
        repositories.py

    schemas/
        github.py
        review.py
        agents.py

    core/
        config.py
        logging.py
        exceptions.py

    prompts/
        security.py
        quality.py
        testing.py
        documentation.py
        aggregator.py

tests/

    unit/
    integration/

alembic/

docker-compose.yml
Dockerfile
.env.example
requirements.txt or pyproject.toml
README.md

Keep modules understandable.

Avoid unnecessary abstractions and excessive design patterns.

---

# 4. GITHUB WEBHOOK

Implement:

POST /webhooks/github

The endpoint receives GitHub webhook events.

Initially support:

- pull_request.opened
- pull_request.synchronize
- pull_request.reopened

Validate the GitHub webhook signature using:

X-Hub-Signature-256

The webhook endpoint should return quickly.

Do not run the entire AI review synchronously inside the request.

Flow:

receive webhook
→ validate signature
→ extract PR information
→ create review job
→ schedule processing
→ return HTTP 202

For the portfolio implementation, Redis-backed asynchronous processing is sufficient.

Do not introduce Kafka, RabbitMQ, Kubernetes, etc.

---

# 5. GITHUB SERVICE

Create a GitHubService responsible for communication with GitHub.

Functions should include concepts such as:

get_pull_request()

get_pull_request_files()

get_file_content()

get_repository_metadata()

post_review_comment()

The service should obtain:

- repository owner
- repository name
- PR number
- base branch
- head branch
- changed files
- patches/diffs
- additions
- deletions

Never place GitHub API logic directly inside AI agents.

---

# 6. PR DIFF REPRESENTATION

Create a normalized internal representation.

Example:

PullRequestContext

repository
pr_number
title
description
base_branch
head_branch
changed_files
total_additions
total_deletions

Each changed file should contain:

filename
status
patch
additions
deletions

Agents should receive this normalized structure rather than raw GitHub webhook JSON.

---

# 7. LANGGRAPH WORKFLOW

This is one of the most important parts of the project.

Implement a five-stage LangGraph workflow.

STAGE 1
INGEST

↓

STAGE 2
CONTEXT RETRIEVAL

↓

STAGE 3
PARALLEL SPECIALIZED AGENTS

Security Agent
Testing Agent
Quality Agent
Documentation Agent

↓

STAGE 4
AGGREGATION

↓

STAGE 5
PUBLISH / PERSIST

Use a shared typed graph state.

Example conceptual state:

class ReviewState(TypedDict):

    review_id: str

    repository: str

    pr_number: int

    pr_context: dict

    retrieved_context: list

    security_findings: list

    quality_findings: list

    testing_findings: list

    documentation_findings: list

    aggregated_review: dict

    errors: list

The four review agents MUST execute independently and, where practical, concurrently.

A failure in one agent should not automatically destroy the entire review.

The aggregator should be capable of producing a partial review if three agents succeed and one fails.

---

# 8. AGENT DESIGN

Implement four specialized agents.

Each agent must have a clearly different responsibility.

Do NOT create four agents that use essentially the same prompt.

Every agent should return STRUCTURED OUTPUT validated using Pydantic.

---

# 9. SECURITY AGENT

Purpose:

Detect security-related problems introduced by the PR.

Look for issues such as:

- SQL injection
- command injection
- hardcoded credentials
- insecure authentication logic
- missing authorization
- unsafe deserialization
- path traversal
- sensitive data exposure
- insecure cryptographic usage
- suspicious user-input handling

Do not claim certainty when evidence is weak.

Output:

SecurityReview

findings: List[Finding]
summary: str
risk_score: int

Each Finding:

file
line_or_section
severity
title
description
suggestion
confidence

Severity:

LOW
MEDIUM
HIGH
CRITICAL

Confidence:

0.0 - 1.0

---

# 10. CODE QUALITY AGENT

Purpose:

Review maintainability and general engineering quality.

Check:

- duplicated code
- overly complex functions
- poor naming
- unnecessary abstractions
- error handling
- dead code
- obvious performance problems
- maintainability
- separation of concerns
- problematic control flow

Avoid meaningless style nitpicks.

Focus on findings that a developer would genuinely care about.

---

# 11. TESTING AGENT

Purpose:

Determine whether changes are adequately tested.

Analyze:

- new branches
- error paths
- edge cases
- changed behavior
- missing unit tests
- missing integration tests
- potentially fragile logic

The agent should identify WHAT behavior needs testing.

Example:

Instead of:

"Add more tests."

Return:

"`login()` now handles expired refresh tokens, but no test covers the expired-token branch."

---

# 12. DOCUMENTATION AGENT

Purpose:

Identify documentation problems.

Check:

- undocumented public APIs
- new environment variables
- configuration changes
- behavior changes
- missing README updates
- confusing comments
- stale comments
- important functions lacking useful documentation

Avoid demanding documentation for trivial implementation details.

---

# 13. COMMON FINDING SCHEMA

All agents should preferably use a compatible Finding schema.

Example:

class Finding(BaseModel):

    category: str

    severity: Literal[
        "LOW",
        "MEDIUM",
        "HIGH",
        "CRITICAL"
    ]

    file: str

    line: Optional[int]

    title: str

    description: str

    suggestion: str

    confidence: float

This makes aggregation easier.

Validate every LLM response.

If structured output parsing fails:

retry once.

If it fails again:

record the agent failure and continue the workflow.

---

# 14. PROMPT ENGINEERING

Create separate prompts for every agent.

Prompts should tell agents:

1. Their specific responsibility
2. What they SHOULD inspect
3. What they SHOULD ignore
4. Required output schema
5. How severity should be assigned
6. How confidence should be interpreted
7. Never invent code that does not exist
8. Only report findings supported by the provided diff/context

A major project goal is reducing noisy AI reviews.

Therefore explicitly instruct agents:

Do not produce findings merely to fill the response.

Returning zero findings is completely valid.

Prefer 2 high-value findings over 10 speculative comments.

---

# 15. CONTEXT RETRIEVAL WITH PGVECTOR

Use PostgreSQL + PGVector to provide repository context.

The system should be capable of storing embeddings for repository code chunks.

Example table:

repository_chunks

id
repository
file_path
content
embedding
created_at

When reviewing a changed file:

1. Generate embedding for relevant changed code
2. Search PGVector for similar repository chunks
3. Retrieve a small number of relevant results
4. Add those snippets to the review context

This allows agents to understand related code outside the PR diff.

Example:

PR modifies:

auth/login.py

Vector retrieval may find:

auth/token_service.py
auth/middleware.py
tests/test_auth.py

Limit retrieval.

Do NOT dump the entire repository into the LLM.

Use something like top 3-5 relevant chunks.

---

# 16. REPOSITORY INDEXING

Create a simple repository indexing mechanism.

Endpoint:

POST /repositories/index

Input:

repository identifier

The indexer:

fetches selected repository files
→ splits files into chunks
→ generates embeddings
→ stores them in PGVector

Support common code files such as:

.py
.js
.ts
.tsx
.jsx
.cpp
.h
.hpp
.java
.go

Skip:

node_modules
.git
build
dist
generated files
binary files
very large files

Do not attempt to build an advanced compiler-aware code indexing system.

Simple function/class or size-based chunking is sufficient.

---

# 17. REDIS

Use Redis for practical purposes.

At minimum:

1. Review job state
2. Temporary PR processing data
3. Caching repeated operations where useful

Example keys:

review:{review_id}:status

github:pr:{repo}:{pr_number}

Possible statuses:

QUEUED
PROCESSING
COMPLETED
PARTIAL
FAILED

Use TTLs for temporary cached data.

Do not use Redis merely so that it appears on the technology list.

---

# 18. POSTGRESQL DATA MODEL

Create sensible tables.

Repositories

id
github_full_name
created_at
updated_at

PullRequests

id
repository_id
pr_number
title
author
base_branch
head_branch
created_at

Reviews

id
pull_request_id
status
risk_score
summary
recommendation
created_at
completed_at

Findings

id
review_id
agent_type
file_path
line_number
severity
title
description
suggestion
confidence

RepositoryChunks

id
repository_id
file_path
content
embedding

Use proper relationships.

Use Alembic migrations.

---

# 19. AGGREGATOR

After all four agents finish, execute an aggregation node.

The aggregator receives all findings.

Responsibilities:

- combine findings
- remove obvious duplicates
- prioritize important issues
- calculate overall risk
- generate concise summary
- generate recommendation

Possible recommendation:

APPROVE

COMMENT

REQUEST_CHANGES

Do not blindly let the LLM determine everything.

Implement deterministic risk logic.

Example scoring:

CRITICAL = 10
HIGH = 5
MEDIUM = 2
LOW = 1

Weight by confidence where useful.

Example:

score = severity_weight * confidence

Use thresholds to determine overall risk.

The LLM may generate the natural-language summary, while deterministic application logic determines the recommendation.

This gives a good interview discussion around combining deterministic systems with probabilistic LLM outputs.

---

# 20. DUPLICATE FINDINGS

Different agents may report the same issue.

Implement lightweight deduplication.

Possible approach:

Normalize:

file + approximate line + title/category

Then optionally use semantic similarity for ambiguous duplicates.

Do not build a complex clustering system.

---

# 21. CHECKPOINTING

Use LangGraph checkpointing.

Persist enough graph state that a failed review does not necessarily need to restart from zero.

Demonstrate:

- shared graph state
- node execution
- retries
- checkpointing
- partial failure recovery

Keep the implementation understandable.

---

# 22. RETRIES

Implement controlled retry logic.

Examples:

GitHub API temporary failure:

retry with exponential backoff.

LLM request:

retry maximum 2-3 times.

Structured output failure:

one repair/retry attempt.

Never implement infinite retries.

Store failures in logs.

---

# 23. REVIEW API

Implement useful endpoints.

GET /health

GET /reviews/{review_id}

GET /reviews/{review_id}/findings

GET /repositories/{repository}/reviews

POST /repositories/index

POST /webhooks/github

Example:

GET /reviews/{review_id}

returns:

{
    "id": "...",
    "status": "COMPLETED",
    "repository": "owner/repository",
    "pr_number": 42,
    "risk": "MEDIUM",
    "recommendation": "COMMENT",
    "summary": "...",
    "findings": [...]
}

---

# 24. GITHUB REVIEW OUTPUT

After aggregation, publish a single clean Markdown comment.

Example:

# 🤖 AI Pull Request Review

**Overall Risk:** Medium  
**Recommendation:** Comment

## 🔐 Security

### HIGH — Potential SQL Injection

`src/users.py:84`

User-controlled `username` is directly interpolated into the SQL query.

**Suggestion**

Use parameterized queries.

## 🧪 Testing

### MEDIUM — Missing failure-path coverage

`tests/test_users.py`

The new duplicate-user handling branch does not appear to have corresponding test coverage.

## 🧹 Code Quality

No significant issues found.

## 📚 Documentation

No significant issues found.

---

**Summary**

2 actionable findings detected across 7 changed files.

Generated by AI PR Review Agent.

Keep comments readable.

Do not dump internal chain-of-thought, raw prompts, token counts, embeddings, or internal graph state into GitHub.

---

# 25. LARGE PR HANDLING

LLM context windows should not be abused.

Implement basic safeguards.

If the PR is small:

analyze normally.

If large:

split by files or logical chunks.

Prioritize changed lines.

Limit context per agent.

For extremely large PRs:

review the most important files and clearly indicate that the review was limited.

Define configurable limits.

Example:

MAX_FILES_PER_REVIEW
MAX_PATCH_CHARS
MAX_CONTEXT_CHUNKS

---

# 26. SECURITY OF THE APPLICATION

At minimum implement:

GitHub webhook signature validation

Environment variables for secrets

No secrets committed to repository

Input validation

Safe database operations

Request timeouts

Basic API error handling

Do not execute code from submitted repositories.

The system reviews source code as TEXT.

Never run arbitrary PR code.

---

# 27. OBSERVABILITY

Implement structured logging.

Each review should have:

review_id

repository

pr_number

agent

stage

duration

status

Example:

{
    "review_id": "...",
    "stage": "security_agent",
    "duration_ms": 1432,
    "status": "completed"
}

Track basic timing metrics internally.

Do not introduce a massive monitoring stack.

Simple structured logs are enough.

---

# 28. SIMULATED LOAD / SCALABILITY DEMO

The project description should reasonably support discussion of scalability.

Create a simple load-testing script or benchmark that can simulate webhook/review API traffic.

The goal is NOT to genuinely run 1000 simultaneous expensive LLM reviews.

Instead demonstrate that the architecture can accept large numbers of incoming requests while work is processed asynchronously.

Measure things like:

request acceptance latency

queue depth

processing throughput

cache hit rate if applicable

Explain clearly in README that load testing uses simulated/mock LLM execution when appropriate.

Do not fabricate benchmark numbers.

---

# 29. TESTING

Testing is important for this project.

Use pytest.

Create unit tests for:

webhook signature validation

GitHub payload parsing

severity scoring

recommendation calculation

finding deduplication

Pydantic output validation

Redis status handling

LangGraph state transitions

Mock external APIs.

Create at least one integration test:

sample GitHub webhook

→ workflow

→ mocked agents

→ aggregation

→ database

→ final review

Do not require real LLM API calls during normal automated tests.

---

# 30. MOCK MODE

Implement:

LLM_MOCK_MODE=true

When enabled, agents return deterministic predefined responses.

This is extremely useful for:

testing

demoing

development

load testing

The entire application should run without spending LLM API credits when mock mode is enabled.

---

# 31. DOCKER

Create Docker configuration.

docker-compose should start:

API

PostgreSQL + PGVector

Redis

The developer should be able to run:

docker compose up --build

Then:

FastAPI:
localhost:8000

Swagger:
localhost:8000/docs

PostgreSQL and Redis should automatically be available to the application.

---

# 32. CONFIGURATION

Create `.env.example`.

Include variables such as:

DATABASE_URL=

REDIS_URL=

GITHUB_TOKEN=

GITHUB_WEBHOOK_SECRET=

LLM_API_KEY=

LLM_MODEL=

LLM_BASE_URL=

LLM_MOCK_MODE=false

EMBEDDING_MODEL=

MAX_FILES_PER_REVIEW=

MAX_PATCH_CHARS=

Never commit real secrets.

---

# 33. README

Create an excellent README.

The README is especially important because interviewers may inspect the repository quickly.

Include:

# AI Pull Request Review Agent

Short project description.

## Architecture

Include an ASCII or Mermaid architecture diagram.

Example:

GitHub
    |
    v
FastAPI Webhook
    |
    v
Redis Job State
    |
    v
LangGraph
    |
    +-----------------------------+
    |        |        |           |
Security   Testing  Quality   Documentation
    |        |        |           |
    +-------------+---------------+
                  |
             Aggregator
                  |
             PostgreSQL
                  |
             GitHub Review

Also show PGVector retrieval.

## Features

## Tech Stack

## LangGraph Workflow

## Agent Responsibilities

## Database Design

## Running Locally

## Environment Variables

## API Endpoints

## Example Review

## Testing

## Design Decisions

## Limitations

## Future Improvements

---

# 34. DESIGN DECISIONS SECTION

Explicitly explain important engineering choices.

Examples:

### Why LangGraph?

The review process is naturally represented as a stateful graph with parallel specialized agents, retries, aggregation, and checkpointing.

### Why Redis?

Webhook handlers should respond quickly rather than waiting for multiple LLM requests. Redis provides lightweight temporary job state and caching.

### Why PostgreSQL?

Reviews, pull requests, findings, and repository metadata are relational.

### Why PGVector?

Semantic retrieval allows agents to inspect relevant repository context without sending the entire codebase to the model.

### Why multiple agents?

Different review categories require different instructions and evaluation criteria. Specialized prompts reduce prompt complexity and make individual reviewers easier to evaluate.

---

# 35. IMPORTANT LIMITATIONS

Be transparent.

Mention:

LLM reviews can produce false positives.

Semantic retrieval is approximate.

The project does not replace human review.

Diff-only analysis may miss runtime behavior.

Large PRs require context truncation.

Generated suggestions should be validated by developers.

This makes the project more credible.

---

# 36. INTERVIEW-FRIENDLY IMPLEMENTATION

Prioritize components that are interesting to discuss in interviews.

The code should make it easy for me to explain:

1. Why I chose LangGraph.
2. How graph state moves through nodes.
3. How agents execute concurrently.
4. What happens when one agent fails.
5. How structured LLM output is validated.
6. How hallucinated findings are reduced.
7. Why PGVector is useful.
8. How repository context is retrieved.
9. Why Redis is used.
10. Why webhook processing is asynchronous.
11. How duplicate findings are removed.
12. How overall risk is calculated.
13. How GitHub authentication works.
14. How webhook signatures are verified.
15. How the system handles large PRs.
16. How retries work.
17. How database data is modeled.
18. How the system could scale horizontally.
19. What bottlenecks would appear first.
20. What I would change for production.

Prefer understandable implementations that I can explain on a whiteboard.

---

# 37. DO NOT OVER-ENGINEER

Do NOT add unless absolutely necessary:

Kubernetes

Kafka

microservices

service mesh

GraphQL

Elasticsearch

complex RBAC

multi-tenant billing

frontend dashboard

custom distributed scheduler

dozens of agents

fine-tuned models

AST support for every language

full GitHub App marketplace installation

enterprise monitoring infrastructure

This is an interview portfolio project.

The objective is:

small codebase + strong architecture + working demo + interesting engineering decisions.

---

# 38. CODE QUALITY REQUIREMENTS

Code should:

use type hints

use async/await correctly

have clear function names

have concise comments

avoid giant functions

avoid excessive abstractions

handle errors explicitly

use dependency injection where useful

use Pydantic models

separate API/business/database/AI responsibilities

Pass linting and tests.

Prefer simple code over clever code.

---

# 39. DEVELOPMENT PLAN

Build this incrementally.

Do NOT attempt the whole application in one giant coding pass.

Follow this order.

PHASE 1 — FOUNDATION

Create:

project structure
FastAPI app
configuration
PostgreSQL
Redis
Docker Compose
health endpoint

Verify everything runs.

PHASE 2 — GITHUB

Implement:

webhook endpoint
signature verification
payload parsing
GitHubService
PR diff retrieval

Test using sample payloads.

PHASE 3 — DATABASE

Create:

models
migrations
repositories
pull requests
reviews
findings

Verify persistence.

PHASE 4 — SINGLE AGENT

Implement Security Agent first.

Add:

prompt
structured output
Pydantic validation
LLM provider
mock mode

Verify end-to-end.

PHASE 5 — LANGGRAPH

Create:

ReviewState
ingestion node
context node
agent node
aggregation node
persistence/publish node

Verify graph execution.

PHASE 6 — MULTI-AGENT

Add:

Quality
Testing
Documentation

Run them in parallel.

Add partial failure handling.

PHASE 7 — PGVECTOR

Implement:

repository indexing
embedding generation
vector storage
similarity retrieval
context injection

PHASE 8 — REDIS / ASYNC PROCESSING

Add:

job state
caching
background processing
status tracking

PHASE 9 — GITHUB OUTPUT

Publish final Markdown review to the PR.

PHASE 10 — TESTS

Build:

unit tests
integration tests
mock LLM tests

PHASE 11 — POLISH

Add:

structured logging
retry logic
large-PR handling
README
architecture diagrams
sample screenshots/demo

---

# 40. IMPLEMENTATION BEHAVIOR

When helping me implement this project:

DO NOT generate the entire repository at once.

Work phase-by-phase.

For each phase:

1. Explain what we are building.
2. Show files being added or modified.
3. Implement them completely.
4. Explain important design decisions.
5. Provide commands required to run it.
6. Provide a small verification procedure.
7. Stop after the phase is working.

Do not move to the next phase until the current architecture is coherent.

If you discover an unnecessary architectural complexity, choose the simpler implementation.

---

# 41. FINAL TARGET

When finished, I should be able to demonstrate:

1. Start the infrastructure using Docker Compose.

2. Open FastAPI Swagger.

3. Send a simulated GitHub PR webhook.

4. Show that a review job was created.

5. Show LangGraph executing the review workflow.

6. Show four specialized agents reviewing the PR.

7. Show relevant repository context retrieved using PGVector.

8. Show aggregated findings.

9. Show the review persisted in PostgreSQL.

10. Show review/job state using Redis.

11. Show the generated GitHub-style review.

12. Run automated tests.

The final repository should feel like a serious student engineering project—not a tutorial CRUD application, but also not an artificially over-engineered enterprise system.

Most importantly:

EVERY major component must have a clear reason for existing.

I need to be able to explain and defend the architecture during a technical interview.

Start with PHASE 1 only.

Before writing code, briefly show the proposed directory structure and explain the request flow. Then implement Phase 1.