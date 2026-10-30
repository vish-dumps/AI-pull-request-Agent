import re
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    StringConstraints,
    model_validator,
)


def validate_repository(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("Repository must be an owner/name identifier")
    if value.split("/", 1)[1] in {".", ".."}:
        raise ValueError("Invalid repository name")
    return value


RepositoryName = Annotated[str, AfterValidator(validate_repository)]
CommitSHA = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{40}$")]
PRNumber = Annotated[int, Field(gt=0, strict=True)]


class GitHubRepository(BaseModel):
    full_name: RepositoryName
    id: int | None = None
    description: str | None = None
    default_branch: str | None = None
    private: bool = False


class GitHubUser(BaseModel):
    login: str = Field(min_length=1)


class GitHubBranch(BaseModel):
    ref: str = Field(min_length=1)
    sha: CommitSHA
    repo: GitHubRepository | None = None


class GitHubPullRequest(BaseModel):
    number: PRNumber
    title: str
    body: str | None = None
    user: GitHubUser
    base: GitHubBranch
    head: GitHubBranch
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    changed_files: int = Field(default=0, ge=0)


class PullRequestWebhook(BaseModel):
    action: str
    number: PRNumber
    repository: GitHubRepository
    pull_request: GitHubPullRequest

    @model_validator(mode="after")
    def check_target(self) -> "PullRequestWebhook":
        if self.number != self.pull_request.number:
            raise ValueError("Pull request numbers disagree")
        base_repository = self.pull_request.base.repo
        if base_repository and (
            base_repository.full_name.lower() != self.repository.full_name.lower()
        ):
            raise ValueError("Pull request does not target the webhook repository")
        return self


class ChangedFile(BaseModel):
    filename: str = Field(min_length=1)
    status: str = Field(min_length=1)
    patch: str | None = None
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    previous_filename: str | None = None


class PullRequestContext(BaseModel):
    repository: RepositoryName
    pr_number: PRNumber
    title: str
    description: str
    author: str
    base_branch: str
    head_branch: str
    base_sha: CommitSHA
    head_sha: CommitSHA
    head_repository: RepositoryName | None
    changed_files: list[ChangedFile]
    total_additions: int = Field(ge=0)
    total_deletions: int = Field(ge=0)
    total_changed_files: int = Field(ge=0)
    files_truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


class GitTreeEntry(BaseModel):
    path: str
    mode: str
    type: str
    sha: CommitSHA
    size: int | None = Field(default=None, ge=0)


class GitTree(BaseModel):
    sha: CommitSHA
    tree: list[GitTreeEntry] = Field(max_length=100000)
    truncated: bool = Field(strict=True)
