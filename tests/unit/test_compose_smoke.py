from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from scripts.compose_smoke import isolated_environment, require_local_endpoint, smoke_config
from scripts.fixture_github import create_fixture_app


def base_config():
    return {
        "name": "existing-project",
        "services": {
            "api": {"environment": {}, "ports": [{"published": "8000"}], "depends_on": {}},
            "worker": {"environment": {}},
            "postgres": {"ports": [{"published": "5432"}]},
            "redis": {"ports": [{"published": "6379"}]},
        },
        "volumes": {"postgres_data": {"name": "existing-data"}},
        "networks": {"default": {"name": "existing-network"}},
    }


def test_smoke_resources_are_isolated_and_credentials_are_fixture_only(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-user-token")
    monkeypatch.setenv("LLM_MOCK_MODE", "false")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "existing-project")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    isolated = isolated_environment()
    assert (
        not {"GITHUB_TOKEN", "LLM_MOCK_MODE", "COMPOSE_PROJECT_NAME", "LANGSMITH_TRACING"}
        & isolated.keys()
    )
    base = base_config()
    original = deepcopy(base)
    configured = smoke_config(base, "prreview-smoke-123", "fixture-secret")
    assert base == original
    assert configured["volumes"]["postgres_data"]["name"] == "prreview-smoke-123_postgres_data"
    assert configured["networks"]["default"]["name"] == "prreview-smoke-123_default"
    assert (
        configured["services"]["postgres"]["ports"]
        == configured["services"]["redis"]["ports"]
        == []
    )
    for name in ("api", "worker"):
        environment = configured["services"][name]["environment"]
        assert environment["GITHUB_API_BASE_URL"] == "http://fixture:8081"
        assert environment["GITHUB_TOKEN"] == "compose-smoke-token"
        assert environment["LLM_MOCK_MODE"] == "true" and environment["LLM_API_KEY"] == ""


@pytest.mark.parametrize("kind", ["volume", "network", "container"])
def test_smoke_rejects_reusing_existing_resources(kind):
    base = base_config()
    if kind == "container":
        base["services"]["api"]["container_name"] = "existing"
    else:
        key = "volumes" if kind == "volume" else "networks"
        next(iter(base[key].values()))["external"] = True
    with pytest.raises(ValueError):
        smoke_config(base, "prreview-smoke-123", "fixture-secret")


def test_fixture_only_serves_synthetic_pr_and_keeps_comment_in_memory():
    with TestClient(create_fixture_app()) as client:
        assert client.get("/user").status_code == 401
        assert (
            client.get("/user", headers={"Authorization": "Bearer compose-smoke-token"}).json()[
                "id"
            ]
            == 17
        )
        assert client.get("/repos/real/private/pulls/1").status_code == 404
        pr = client.get("/repos/demo/multi-agent-review/pulls/6").json()
        assert pr["state"] == "open" and pr["changed_files"] == 2
        assert len(client.get("/repos/demo/multi-agent-review/pulls/6/files").json()) == 2
        path = "/repos/demo/multi-agent-review/issues/6/comments"
        assert client.post(path, json={"body": ""}).status_code == 422
        posted = client.post(path, json={"body": "Synthetic comment"})
        assert posted.status_code == 201
        assert client.get(path).json() == [posted.json()]
        assert client.get("/fixture/stats").json()["posts"] == 1


def test_smoke_requires_local_docker_before_starting_resources():
    for endpoint in (
        "npipe:////./pipe/docker_engine",
        "unix:///var/run/docker.sock",
        "tcp://127.0.0.1:2375",
    ):
        require_local_endpoint(endpoint)
    for endpoint in ("ssh://remote", "tcp://remote:2375", "tcp://127.0.0.1.attacker.invalid:2375"):
        with pytest.raises(ValueError, match="local Docker"):
            require_local_endpoint(endpoint)
