class GitHubAPIError(Exception):
    """A GitHub request failed; the message deliberately excludes response contents."""

    def __init__(
        self, message: str, status_code: int | None = None, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class GitHubRateLimitError(GitHubAPIError):
    """Stop requesting until GitHub's rate-limit window has passed."""

    def __init__(
        self, message: str, status_code: int | None = None, retry_after_seconds: float = 60
    ) -> None:
        super().__init__(message, status_code, retry_after_seconds)


class GitHubFileUnsupportedError(GitHubAPIError):
    """A file cannot be used as bounded UTF-8 source input."""


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


class EmbeddingError(Exception):
    """Safe embedding-provider failure without source text or response bodies."""


class RepositoryIndexError(Exception):
    """A bounded repository indexing operation could not complete."""


class LeaseLostError(Exception):
    """A worker no longer owns the queue receipt and must stop processing it."""
