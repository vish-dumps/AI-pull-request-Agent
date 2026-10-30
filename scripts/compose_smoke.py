"""Build and check an isolated Docker stack, using only synthetic GitHub/model data."""

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def isolated_environment():
    fields = {name.casefold() for name in Settings.model_fields}
    return {
        name: value
        for name, value in os.environ.items()
        if name.casefold() not in fields
        and not name.startswith(("COMPOSE_", "LANGCHAIN_", "LANGSMITH_"))
    }


def require_local_endpoint(endpoint):
    if endpoint.startswith(("npipe://", "unix://")):
        return
    parsed = urlsplit(endpoint)
    if parsed.scheme == "tcp" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return
    raise ValueError("The smoke test requires a local Docker endpoint")


def smoke_config(base, project, secret):
    """Canonical standalone config avoids Compose port-list merging and shared names."""
    config = json.loads(json.dumps(base))
    config["name"] = project
    image = f"{project}:local"
    for group in ("volumes", "networks"):
        for name, resource in config.get(group, {}).items():
            if resource.get("external"):
                raise ValueError("Smoke configuration cannot reuse external resources")
            resource["name"] = f"{project}_{name}"
    for name, service in config["services"].items():
        if service.get("container_name"):
            raise ValueError("Smoke configuration cannot use fixed container names")
        service["ports"] = []
        if name in {"api", "worker"}:
            service["image"] = image
            service["environment"].update(
                {
                    "GITHUB_API_BASE_URL": "http://fixture:8081",
                    "GITHUB_TOKEN": "compose-smoke-token",
                    "GITHUB_WEBHOOK_SECRET": secret,
                    "GITHUB_PUBLISH_ENABLED": "true",
                    "GITHUB_PUBLISH_MOCK_REVIEWS": "true",
                    "LLM_MOCK_MODE": "true",
                    "LLM_API_KEY": "",
                    "LLM_BASE_URL": "",
                    "LLM_MODEL": "",
                    "EMBEDDING_MOCK_MODE": "true",
                    "EMBEDDING_API_KEY": "",
                    "EMBEDDING_BASE_URL": "",
                    "RETRIEVAL_ENABLED": "false",
                    "LANGSMITH_TRACING": "false",
                    "LANGCHAIN_TRACING_V2": "false",
                }
            )
    config["services"]["api"]["ports"] = [
        {"target": 8000, "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}
    ]
    config["services"]["fixture"] = {
        "image": image,
        "pull_policy": "never",
        "command": ["python", "scripts/fixture_github.py", "--host", "0.0.0.0"],
        "ports": [{"target": 8081, "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}],
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://localhost:8081/health',timeout=3)",
            ],
            "interval": "2s",
            "timeout": "4s",
            "retries": 20,
        },
        "networks": {"default": None},
    }
    config["services"]["api"]["depends_on"]["fixture"] = {"condition": "service_healthy"}
    return config


def wait_for(check, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.5)
    raise TimeoutError("Smoke check did not reach the required state")


def check_stack(compose):
    api = "http://" + compose("port", "api", "8000").strip()
    fixture = "http://" + compose("port", "fixture", "8081").strip()
    with httpx.Client(timeout=10, trust_env=False) as client:
        healthy = client.get(api + "/health")
        assert healthy.status_code == 200 and all(
            value == "up" for value in healthy.json()["dependencies"].values()
        )
        pr = client.get(fixture + "/repos/demo/multi-agent-review/pulls/6").json()
        body = json.dumps(
            {
                "action": "opened",
                "number": 6,
                "repository": {"full_name": "demo/multi-agent-review"},
                "pull_request": pr,
            }
        ).encode()
        # The secret is supplied by the caller; no user's application config is read.
        return api, fixture, body


def exercise(compose, secret):
    api, fixture, body = check_stack(compose)
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": str(uuid4()),
        "X-Hub-Signature-256": signature,
    }
    with httpx.Client(timeout=10, trust_env=False) as client:
        assert (
            client.post(
                api + "/webhooks/github",
                content=body,
                headers={**headers, "X-Hub-Signature-256": "sha256=" + "0" * 64},
            ).status_code
            == 403
        )
        accepted = client.post(api + "/webhooks/github", content=body, headers=headers)
        assert accepted.status_code == 202
        review_id = accepted.json()["review_id"]

        def terminal():
            status = client.get(f"{api}/reviews/{review_id}/status").json()
            return status if status.get("status") in {"COMPLETED", "PARTIAL", "FAILED"} else None

        assert wait_for(terminal)["status"] == "COMPLETED"
        stored = client.get(f"{api}/reviews/{review_id}").json()
        assert len(stored["findings"]) == 4 and abs(stored["risk_score"] - 7.7) < 0.0001
        publication = client.get(f"{api}/reviews/{review_id}/publication").json()
        assert publication["status"] == "PUBLISHED"
        preview = client.get(f"{api}/reviews/{review_id}/comment").text
        stats = client.get(fixture + "/fixture/stats").json()
        assert (
            stats["posts"] == 1
            and preview == stats["comments"][0]["body"]
            and "Demo review" in preview
        )
        repeat = client.post(api + "/webhooks/github", content=body, headers=headers).json()
        assert repeat["duplicate"] and repeat["review_id"] == review_id
        compose("restart", "worker")
        assert client.get(fixture + "/fixture/stats").json()["posts"] == 1
        compose("stop", "redis")
        degraded = client.get(api + "/health")
        assert degraded.status_code == 503 and degraded.json()["dependencies"]["redis"] == "down"
        compose("start", "redis")
        wait_for(lambda: client.get(api + "/health").status_code == 200)
        assert client.get(f"{api}/reviews/{review_id}").json()["status"] == "COMPLETED"
        assert client.get(f"{api}/reviews/{review_id}/status").json()["status"] == "COMPLETED"
        replay = client.post(api + "/webhooks/github", content=body, headers=headers).json()
        assert replay["duplicate"] and replay["review_id"] == review_id
        assert client.get(fixture + "/fixture/stats").json()["posts"] == 1
        compose("exec", "-T", "api", "alembic", "check")
        compose("exec", "-T", "api", "python", "-m", "pip", "check")
        runtime = json.loads(
            compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import json,sys,importlib.metadata as m; "
                "print(json.dumps({'python':sys.version.split()[0], 'packages': "
                "{n:m.version(n) for n in "
                "['fastapi','langgraph','pydantic','sqlalchemy','redis']}}))",
            )
        )
        postgres = compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "review_agent",
            "-d",
            "review_agent",
            "-Atc",
            "SELECT current_setting('server_version'), "
            "(SELECT extversion FROM pg_extension WHERE extname='vector')",
        ).strip()
        return {
            "review_id": review_id,
            "status": "COMPLETED",
            "findings": 4,
            "risk_score": 7.7,
            "fixture_comments": 1,
            "signature_rejection": True,
            "redelivery_deduplicated": True,
            "redis_readiness_recovered": True,
            "mock_models": True,
            "github": "isolated fixture container",
            "real_github_writes": False,
            "runtime": runtime,
            "postgres_and_vector": postgres,
            "migration_drift": False,
            "dependency_conflicts": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Validate/write configuration without starting containers",
    )
    args = parser.parse_args()
    environment = isolated_environment()
    project = "prreview-smoke-" + uuid4().hex[:12]
    directory = ROOT / "artifacts" / project
    directory.mkdir(parents=True)
    secret = uuid4().hex
    base = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / ".env.example"),
            "-f",
            str(ROOT / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        env=environment,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    config = smoke_config(json.loads(base.stdout), project, secret)
    path = directory / "compose.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    command = ["docker", "compose", "-p", project, "-f", str(path)]

    def compose(*arguments):
        sequence = len(list(directory.glob("command-*.stdout.log")))
        stdout = directory / f"command-{sequence:03d}.stdout.log"
        stderr = directory / f"command-{sequence:03d}.stderr.log"
        with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
            result = subprocess.run(
                [*command, *arguments],
                env=environment,
                cwd=ROOT,
                stdout=out,
                stderr=err,
                timeout=900,
            )
        output = stdout.read_text(encoding="utf-8", errors="replace")
        with (directory / "commands.log").open("a", encoding="utf-8") as log:
            log.write(
                " ".join(arguments)
                + "\n"
                + output
                + stderr.read_text(encoding="utf-8", errors="replace")
                + "\n"
            )
        result.check_returncode()
        return output

    compose("config", "--quiet")
    if args.prepare_only:
        print(
            json.dumps({"project": project, "configuration": str(path), "started": False}, indent=2)
        )
        return
    context = subprocess.run(
        ["docker", "context", "inspect"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    endpoint = json.loads(context.stdout)[0]["Endpoints"]["docker"]["Host"]
    if environment.get("DOCKER_HOST") and not environment.get("DOCKER_CONTEXT"):
        endpoint = environment["DOCKER_HOST"]
    require_local_endpoint(endpoint)
    engine = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    information = json.loads(engine.stdout)
    if information["OSType"] != "linux":
        raise ValueError("The application images require Linux containers")
    # An unpredictable new project owns all resources it will later remove.
    existing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if existing.stdout.strip():
        raise RuntimeError("Refusing to reuse an existing Compose project")
    report = {
        "project": project,
        "recorded_at": datetime.now(UTC).isoformat(),
        "successful": False,
        "docker_engine": information["ServerVersion"],
        "container_os": information["OSType"],
    }
    try:
        print(f"Building isolated Docker stack: {project}", flush=True)
        compose("up", "--build", "--detach", "--wait", "--wait-timeout", "240")
        report.update(exercise(compose, secret))
        report["successful"] = True
    finally:
        try:
            (directory / "containers.log").write_text(
                compose("logs", "--no-color"), encoding="utf-8"
            )
        finally:
            compose("down", "--volumes", "--remove-orphans")
            report["resources_removed"] = True
            (directory / "result.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
