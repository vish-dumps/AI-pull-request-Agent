import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

LOG_FIELDS = (
    "dependency",
    "error_type",
    "review_id",
    "repository",
    "pr_number",
    "stage",
    "duration_ms",
    "status",
    "attempt",
    "agent",
    "cache_hit",
    "files_reviewed",
    "files_omitted",
    "patch_chars",
)
_context: ContextVar[dict[str, object] | None] = ContextVar("review_log_context", default=None)


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Correlate nested I/O and parallel reviewers without sharing mutable state."""
    token = _context.set(
        {**(_context.get() or {}), **{k: v for k, v in fields.items() if k in LOG_FIELDS}}
    )
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = _context.get() or {}
        for field in LOG_FIELDS:
            if field in context:
                payload[field] = context[field]
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        return json.dumps(payload)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # Transport/SQL debug logs can contain credential-bearing URLs or bound data.
    for name in ("httpx", "httpcore", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
