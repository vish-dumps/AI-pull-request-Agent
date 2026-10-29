import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db.repositories import ReviewRepository
from app.main import create_app


@pytest.fixture
def review_client(database, settings, resources):
    with TestClient(create_app(settings)) as client:
        client.app.state.session_factory = database.sessions
        yield client


def persist(database, job, context, result=None):
    async def run():
        async with database.sessions.begin() as session:
            store = ReviewRepository(session)
            await store.create_review(job, context)
            if result is not None:
                await store.mark_processing(job.review_id)
                await store.complete_review(job.review_id, result)

    asyncio.run(run())


def test_completed_review_api_returns_persisted_finding(
    review_client, database, sample_job, sample_context, sample_result
):
    persist(database, sample_job, sample_context, sample_result)
    response = review_client.get(f"/reviews/{sample_job.review_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(sample_job.review_id)
    assert data["repository"] == "octocat/hello-world"
    assert data["status"] == "COMPLETED"
    assert data["risk"] == "HIGH"
    assert data["recommendation"] == "REQUEST_CHANGES"
    assert data["completed_at"] is not None
    assert data["findings"][0]["agent_type"] == "security"
    assert data["findings"][0]["line_number"] == 8
    assert "error_message" not in data
    findings = review_client.get(f"/reviews/{sample_job.review_id}/findings")
    assert findings.status_code == 200
    assert findings.json() == data["findings"]


def test_queued_review_exposes_null_result_fields(
    review_client, database, sample_job, sample_context
):
    persist(database, sample_job, sample_context)
    response = review_client.get(f"/reviews/{sample_job.review_id}")
    data = response.json()
    assert response.status_code == 200
    assert data["status"] == "QUEUED"
    assert data["risk"] is data["summary"] is data["recommendation"] is None
    assert data["completed_at"] is None
    assert data["findings"] == []


def test_repository_history_has_case_insensitive_pagination(
    review_client, database, sample_job, sample_context
):
    for _ in range(3):
        persist(
            database,
            sample_job.model_copy(update={"review_id": uuid4(), "delivery_id": uuid4()}),
            sample_context,
        )
    first = review_client.get("/repositories/Octocat/Hello-World/reviews?limit=2").json()
    second = review_client.get("/repositories/octocat/hello-world/reviews?limit=2&offset=2").json()
    assert len(first) == 2
    assert len(second) == 1
    assert len({review["id"] for review in first + second}) == 3
    assert all("findings" not in review for review in first + second)
    assert review_client.get("/repositories/other/repo/reviews").json() == []


@pytest.mark.parametrize("suffix", ["", "/findings"])
def test_unknown_review_returns_404(review_client, suffix):
    assert review_client.get(f"/reviews/{uuid4()}{suffix}").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "/reviews/not-a-uuid",
        "/repositories/o/r/reviews?limit=0",
        "/repositories/o/r/reviews?limit=101",
        "/repositories/o/r/reviews?offset=-1",
        "/repositories/invalid_owner/r/reviews",
    ],
)
def test_invalid_review_queries_are_rejected(review_client, url):
    assert review_client.get(url).status_code == 422


def test_database_failure_returns_sanitized_503(review_client):
    def broken_factory():
        raise OperationalError("SELECT", {}, Exception("private-password"))

    review_client.app.state.session_factory = broken_factory
    response = review_client.get(f"/reviews/{uuid4()}")
    assert response.status_code == 503
    assert response.json() == {"detail": "Review database unavailable"}
