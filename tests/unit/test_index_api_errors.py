from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api.routes.repositories import indexing_services
from app.core.exceptions import EmbeddingError, GitHubAPIError, GitHubRateLimitError
from app.main import create_app


@pytest.mark.parametrize(
    "failure, status",
    [
        (EmbeddingError("private-response"), 502),
        (GitHubAPIError("private-response", 404), 404),
        (GitHubRateLimitError("private-response", 429), 503),
        (OperationalError("private-response", None, Exception()), 503),
        (TimeoutError("private-response"), 504),
    ],
)
def test_index_endpoint_errors_do_not_expose_source_or_internal_data(
    settings, resources, failure, status
):
    app = create_app(settings)

    async def services():
        yield SimpleNamespace(index_github=AsyncMock(side_effect=failure)), None

    app.dependency_overrides[indexing_services] = services
    with TestClient(app) as client:
        response = client.post("/repositories/index", json={"repository": "demo/repo"})
        assert response.status_code == status
        assert "private-response" not in response.text


def test_index_endpoint_reports_missing_embedding_configuration(settings, resources):
    configured = settings.model_copy(update={"embedding_mock_mode": False})
    with TestClient(create_app(configured)) as client:
        response = client.post("/repositories/index", json={"repository": "demo/repo"})
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]
