class GitHubAPIError(Exception):
    """A GitHub request failed; the message deliberately excludes response contents."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubRateLimitError(GitHubAPIError):
    """Stop requesting until GitHub's rate-limit window has passed."""


class PullRequestChangedError(GitHubAPIError):
    """The queued revision or PR snapshot changed during ingestion."""


class ReviewConflictError(Exception):
    """A job identifier was reused with different review inputs."""


class ReviewStateError(Exception):
    """A review cannot enter the requested state."""


class LLMProviderError(Exception):
    """A controlled provider failure; messages exclude raw requests and responses."""


class WorkflowConflictError(Exception):
    """A checkpoint thread was reused with incompatible input or configuration."""


class WorkflowPersistenceError(Exception):
    """A database node failed; its checkpoint can be inspected and resumed."""


class WorkflowTransientError(WorkflowPersistenceError):
    """A temporary database failure eligible for a bounded node retry."""
