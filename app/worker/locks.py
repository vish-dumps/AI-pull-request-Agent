import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@asynccontextmanager
async def review_lock(engine: AsyncEngine, review_id: UUID) -> AsyncIterator[bool]:
    """Session advisory lock, on a dedicated connection without an open transaction."""
    if engine.dialect.name != "postgresql":
        raise ValueError("Worker ownership requires PostgreSQL advisory locks")
    key = int.from_bytes(
        hashlib.sha256(b"review-worker:" + review_id.bytes).digest()[:8], "big", signed=True
    )
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        owned = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        try:
            yield bool(owned)
        finally:
            if owned:
                try:
                    async with asyncio.timeout(5):
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                        )
                except BaseException:
                    # Never return a session-level lock to the pool on cancellation/error.
                    await connection.invalidate()
                    raise
