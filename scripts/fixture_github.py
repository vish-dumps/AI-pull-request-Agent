"""Synthetic GitHub HTTP service for the isolated Compose smoke test only."""

import argparse
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.schemas.github import PullRequestContext


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=60000)


def create_fixture_app():
    context = PullRequestContext.model_validate_json(
        (Path(__file__).resolve().parents[1] / "examples/multi_agent_pr.json").read_bytes()
    )
    application = FastAPI(title="Synthetic GitHub fixture; no upstream forwarding")
    comments = []
    calls = {"pr_reads": 0, "file_reads": 0, "posts": 0}

    def require_target(owner, repository, number):
        if f"{owner}/{repository}" != context.repository or number != context.pr_number:
            raise HTTPException(404, "Only the synthetic fixture PR is available")

    @application.get("/health")
    def health():
        return {"status": "fixture"}

    @application.get("/fixture/stats")
    def stats():
        return {**calls, "comments": comments}

    @application.get("/user")
    def user(authorization: str | None = Header(default=None)):
        if authorization != "Bearer compose-smoke-token":
            raise HTTPException(401, "Fixture token required")
        return {"id": 17, "login": "fixture-reviewer"}

    @application.get("/repos/{owner}/{repository}/pulls/{number}")
    def pull_request(owner: str, repository: str, number: int):
        require_target(owner, repository, number)
        calls["pr_reads"] += 1
        return {
            "number": context.pr_number,
            "title": context.title,
            "body": context.description,
            "state": "open",
            "merged": False,
            "user": {"login": context.author},
            "base": {
                "ref": context.base_branch,
                "sha": context.base_sha,
                "repo": {"full_name": context.repository},
            },
            "head": {
                "ref": context.head_branch,
                "sha": context.head_sha,
                "repo": {"full_name": context.repository},
            },
            "additions": context.total_additions,
            "deletions": context.total_deletions,
            "changed_files": context.total_changed_files,
        }

    @application.get("/repos/{owner}/{repository}/pulls/{number}/files")
    def files(owner: str, repository: str, number: int):
        require_target(owner, repository, number)
        calls["file_reads"] += 1
        return [file.model_dump() for file in context.changed_files]

    @application.get("/repos/{owner}/{repository}/issues/{number}/comments")
    def list_comments(owner: str, repository: str, number: int):
        require_target(owner, repository, number)
        return comments

    @application.post("/repos/{owner}/{repository}/issues/{number}/comments", status_code=201)
    def post_comment(owner: str, repository: str, number: int, data: CommentBody):
        require_target(owner, repository, number)
        calls["posts"] += 1
        comment = {"id": 100 + calls["posts"], "body": data.body, "user": {"id": 17}}
        comments.append(comment)
        return comment

    return application


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    uvicorn.run(create_fixture_app(), host=args.host, port=args.port, log_level="warning")
