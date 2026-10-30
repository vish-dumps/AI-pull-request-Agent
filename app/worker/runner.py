import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from time import perf_counter

from app.core.config import Settings
from app.core.exceptions import (
    GitHubAPIError,
    GitHubRateLimitError,
    LeaseLostError,
    PullRequestChangedError,
    ReviewConflictError,
    WorkflowConflictError,
)
from app.core.logging import log_context
from app.schemas.publication import PublicationStatus
from app.schemas.review import ReviewStatus
from app.services.publication_service import aware
from app.services.worker_queue import Receipt, WorkerQueue
from app.worker.processor import WorkflowProcessor

logger = logging.getLogger(__name__)


def failure_reason(error: Exception) -> tuple[str, bool]:
    if isinstance(error, PullRequestChangedError):
        return "Pull request revision changed; a new delivery is required", False
    if isinstance(error, (ReviewConflictError, WorkflowConflictError)):
        return "Job or checkpoint configuration conflicts with this worker", False
    if (
        isinstance(error, GitHubAPIError)
        and error.retry_after_seconds is not None
        and error.retry_after_seconds > 604800
    ):
        return "GitHub requested a retry outside the worker scheduling window", False
    if isinstance(error, GitHubRateLimitError):
        return "GitHub rate limit reached; retry deferred", True
    if (
        isinstance(error, GitHubAPIError)
        and error.status_code is not None
        and error.status_code < 500
    ):
        return "GitHub rejected PR ingestion", False
    if isinstance(error, TimeoutError):
        return "Review job exceeded its deadline", True
    return "Review infrastructure failed; retry deferred", True


class ReviewWorker:
    def __init__(
        self, queue: WorkerQueue, processor: WorkflowProcessor, settings: Settings
    ) -> None:
        self.queue, self.processor, self.settings = queue, processor, settings

    async def _heartbeat(self, receipt: Receipt) -> None:
        while True:
            await asyncio.sleep(self.settings.worker_lease_seconds / 3)
            async with asyncio.timeout(self.settings.redis_operation_timeout_seconds):
                await self.queue.renew(receipt)

    async def _finish(
        self, receipt: Receipt, terminal: ReviewStatus, *, error: str = "", dead: bool = False
    ) -> None:
        if terminal in {ReviewStatus.COMPLETED, ReviewStatus.PARTIAL}:
            publication = await self.processor.finalize(receipt.job)
            if publication is not None and publication.status == PublicationStatus.PENDING:
                delay = (
                    max(1, (aware(publication.next_retry_at) - datetime.now(UTC)).total_seconds())
                    if publication.next_retry_at
                    else 1
                )
                await self.queue.defer(
                    receipt, delay, "Review saved; GitHub publication retry scheduled"
                )
                return
            if publication is not None and publication.status in {
                PublicationStatus.FAILED,
                PublicationStatus.UNCERTAIN,
            }:
                error = "Review saved; GitHub publication requires attention"
        await self.queue.finish(receipt, terminal, error=error, dead=dead)

    async def _process(self, receipt: Receipt) -> None:
        with log_context(
            review_id=str(receipt.job.review_id),
            repository=receipt.job.repository,
            pr_number=receipt.job.pr_number,
            stage="worker",
        ):
            await self._process_owned(receipt)

    async def _process_owned(self, receipt: Receipt) -> None:
        started = perf_counter()
        async with self.processor.lock(receipt.job) as locked:
            if not locked:
                await self.queue.defer(
                    receipt,
                    self.settings.worker_poll_seconds,
                    "Waiting for the previous review owner",
                )
                return
            try:
                terminal = await self.processor.terminal_status(receipt.job)
            except ReviewConflictError:
                await self.queue.reject(receipt)
                return
            if terminal is not None:
                await self._finish(receipt, terminal)
                return
            cached_status = await self.queue.status(receipt.job.review_id)
            if cached_status is not None and cached_status.status in {
                ReviewStatus.COMPLETED,
                ReviewStatus.PARTIAL,
                ReviewStatus.FAILED,
            }:
                await self._finish(receipt, cached_status.status, error=cached_status.error or "")
                return
            attempts = await self.queue.attempts(receipt)
            if attempts >= self.settings.worker_max_attempts:
                terminal = await self.processor.fail(
                    receipt.job, "Review worker attempts exhausted"
                )
                await self._finish(
                    receipt,
                    terminal,
                    error="Review worker attempts exhausted"
                    if terminal == ReviewStatus.FAILED
                    else "",
                    dead=terminal == ReviewStatus.FAILED,
                )
                return
            attempts = await self.queue.begin(receipt)
            try:
                async with asyncio.timeout(self.settings.worker_job_timeout_seconds):
                    terminal = await self.processor.execute(receipt.job)
            except LeaseLostError:
                raise
            except ReviewConflictError:
                await self.queue.reject(receipt)
                return
            except Exception as error:
                message, retry = failure_reason(error)
                logger.warning(
                    "Review job attempt failed",
                    extra={
                        "stage": "worker",
                        "review_id": str(receipt.job.review_id),
                        "attempt": attempts,
                        "error_type": type(error).__name__,
                    },
                )
                if retry and attempts < self.settings.worker_max_attempts:
                    delay = min(3600, self.settings.worker_retry_seconds * 2 ** (attempts - 1))
                    if isinstance(error, GitHubAPIError) and error.retry_after_seconds is not None:
                        delay = max(delay, error.retry_after_seconds)
                    if isinstance(error, GitHubRateLimitError):
                        delay = max(60 * 2 ** (attempts - 1), delay, error.retry_after_seconds)
                    await self.queue.defer(receipt, delay, message)
                    return
                terminal = await self.processor.fail(receipt.job, message)
                await self._finish(
                    receipt,
                    terminal,
                    error=message if terminal == ReviewStatus.FAILED else "",
                    dead=terminal == ReviewStatus.FAILED,
                )
                return
            await self._finish(
                receipt,
                terminal,
                error="All reviewers failed" if terminal == ReviewStatus.FAILED else "",
            )
        logger.info(
            "Review job finalized",
            extra={
                "stage": "worker",
                "review_id": str(receipt.job.review_id),
                "repository": receipt.job.repository,
                "pr_number": receipt.job.pr_number,
                "status": terminal.value,
                "attempt": attempts,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )

    async def run_once(self) -> bool:
        await self.queue.maintain()
        receipt = await self.queue.claim()
        if receipt is None:
            return False
        work = asyncio.create_task(self._process(receipt))
        heartbeat = asyncio.create_task(self._heartbeat(receipt))
        try:
            done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                await heartbeat
                raise LeaseLostError("Heartbeat ended unexpectedly")
        finally:
            work.cancel()
            heartbeat.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)
            # On cancellation/outage, leave the durable receipt for expiry/recovery.
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            work = asyncio.create_task(self.run_once())
            stopping = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(work), self.settings.worker_shutdown_grace_seconds
                        )
                    except TimeoutError:
                        work.cancel()
                    return
                try:
                    processed = await work
                except Exception as error:
                    logger.warning(
                        "Worker iteration interrupted; receipts remain recoverable",
                        extra={"stage": "worker", "error_type": type(error).__name__},
                    )
                    processed = False
                if not processed:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), self.settings.worker_poll_seconds)
            finally:
                work.cancel()
                stopping.cancel()
                await asyncio.gather(work, stopping, return_exceptions=True)
