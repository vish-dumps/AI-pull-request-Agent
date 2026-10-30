import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import Settings
from app.main import create_app
from app.schemas.github import ChangedFile, PullRequestContext
from app.schemas.jobs import ReviewJob
from app.schemas.review import FindingInput, ReviewCompletion


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:test@localhost/test",
        redis_url="redis://localhost:6379/15",
        health_check_timeout_seconds=0.05,
        log_level="CRITICAL",
        github_webhook_secret="test-webhook-secret",
        github_token="",
    )


@pytest.fixture
def resources(monkeypatch):
    connection = AsyncMock(spec=AsyncConnection)
    connection.scalar.return_value = True
    engine = Mock(spec=AsyncEngine)
    context = MagicMock()
    context.__aenter__.return_value = connection
    engine.connect.return_value = context
    engine.dispose = AsyncMock()
    redis = Mock(spec=Redis)
    redis.ping = AsyncMock(return_value=True)
    redis.aclose = AsyncMock()
    monkeypatch.setattr("app.main.build_engine", lambda settings: engine)
    monkeypatch.setattr("app.main.Redis.from_url", lambda *args, **kwargs: redis)
    return engine, connection, redis


@pytest.fixture
def client(settings, resources):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def sample_payload():
    sample = Path(__file__).resolve().parents[1] / "examples/pull_request.opened.json"
    return json.loads(sample.read_text(encoding="utf-8"))


@pytest.fixture
def sample_job(sample_payload):
    pr = sample_payload["pull_request"]
    return ReviewJob(
        delivery_id=UUID("00000000-0000-4000-8000-000000000001"),
        repository=sample_payload["repository"]["full_name"],
        pr_number=pr["number"],
        action=sample_payload["action"],
        base_sha=pr["base"]["sha"],
        head_sha=pr["head"]["sha"],
    )


@pytest.fixture
def sample_context(sample_payload):
    pr = sample_payload["pull_request"]
    return PullRequestContext(
        repository=sample_payload["repository"]["full_name"],
        pr_number=pr["number"],
        title=pr["title"],
        description=pr["body"],
        author=pr["user"]["login"],
        base_branch=pr["base"]["ref"],
        head_branch=pr["head"]["ref"],
        base_sha=pr["base"]["sha"],
        head_sha=pr["head"]["sha"],
        head_repository=pr["head"]["repo"]["full_name"],
        changed_files=[
            ChangedFile(
                filename="users.py",
                status="modified",
                additions=8,
                deletions=1,
                patch="@@ -1 +1 @@\n-old\n+new",
            )
        ],
        total_additions=8,
        total_deletions=1,
        total_changed_files=1,
    )


@pytest.fixture
def sample_result():
    return ReviewCompletion(
        risk="HIGH",
        risk_score=4.5,
        summary="Synthetic fixture: validate user input.",
        recommendation="REQUEST_CHANGES",
        findings=[
            FindingInput(
                agent_type="security",
                file_path="users.py",
                line_number=8,
                severity="HIGH",
                title="Synthetic SQL injection example",
                description="Demo query uses user input.",
                suggestion="Use query parameters.",
                confidence=0.9,
            )
        ],
    )


@pytest.fixture
def security_context():
    sample = Path(__file__).resolve().parents[1] / "examples/security_pr.json"
    return PullRequestContext.model_validate_json(sample.read_bytes())


@pytest.fixture
def security_output():
    return {
        "findings": [
            {
                "agent_type": "security",
                "file_path": "src/users.py",
                "line_number": 2,
                "severity": "HIGH",
                "title": "SQL query interpolates user input",
                "description": "The added query includes the username directly.",
                "suggestion": "Use parameterized queries.",
                "confidence": 0.9,
                "evidence": "query = f\"SELECT * FROM users WHERE name = '{username}'\"",
            }
        ],
        "summary": "One supported security issue.",
        "risk_score": 70,
    }


@pytest.fixture
def security_job(security_context):
    return ReviewJob(
        delivery_id=uuid4(),
        repository=security_context.repository,
        pr_number=security_context.pr_number,
        action="opened",
        base_sha=security_context.base_sha,
        head_sha=security_context.head_sha,
    )


@pytest.fixture
def multi_context():
    sample = Path(__file__).resolve().parents[1] / "examples/multi_agent_pr.json"
    return PullRequestContext.model_validate_json(sample.read_bytes())


@pytest.fixture
def multi_job(multi_context):
    return ReviewJob(
        delivery_id=uuid4(),
        repository=multi_context.repository,
        pr_number=multi_context.pr_number,
        action="opened",
        base_sha=multi_context.base_sha,
        head_sha=multi_context.head_sha,
    )
