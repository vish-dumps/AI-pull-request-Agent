import asyncio
import json
import logging
from time import perf_counter
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.core.security import verify_github_signature
from app.schemas.github import PullRequestWebhook
from app.schemas.jobs import (
    SUPPORTED_PR_ACTIONS,
    ReviewJob,
    WebhookAccepted,
    WebhookIgnored,
)
from app.services.job_queue import ReviewJobQueue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def read_webhook_body(request: Request, max_bytes: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(413, "Webhook payload is too large")
        body.extend(chunk)
    return bytes(body)


@router.post(
    "/github",
    status_code=202,
    response_model=WebhookAccepted | WebhookIgnored,
    responses={200: {"model": WebhookIgnored}, 403: {"description": "Invalid signature"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}},
            "description": "GitHub pull_request JSON; sign the exact request bytes.",
        }
    },
)
async def github_webhook(
    request: Request,
    response: Response,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
    event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
) -> WebhookAccepted | WebhookIgnored:
    started = perf_counter()
    settings = request.app.state.settings
    secret = settings.github_webhook_secret.get_secret_value()
    if not secret:
        raise HTTPException(503, "GitHub webhook secret is not configured")
    body = await read_webhook_body(request, settings.max_webhook_body_bytes)
    if not verify_github_signature(body, secret, signature):
        raise HTTPException(403, "Invalid webhook signature")
    if not event:
        raise HTTPException(400, "X-GitHub-Event is required")
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "Webhook content type must be application/json")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "Invalid JSON payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(422, "Webhook payload must be an object")
    if event != "pull_request":
        response.status_code = 200
        return WebhookIgnored(reason="Unsupported event")
    action = payload.get("action")
    if not isinstance(action, str):
        raise HTTPException(422, "Pull request action is required")
    if action not in SUPPORTED_PR_ACTIONS:
        response.status_code = 200
        return WebhookIgnored(reason="Unsupported pull request action")
    try:
        delivery_id = UUID(delivery) if delivery else None
    except ValueError:
        raise HTTPException(422, "X-GitHub-Delivery must be a UUID") from None
    if delivery_id is None:
        raise HTTPException(422, "X-GitHub-Delivery is required")
    try:
        webhook = PullRequestWebhook.model_validate(payload)
        job = ReviewJob(
            delivery_id=delivery_id,
            repository=webhook.repository.full_name,
            pr_number=webhook.number,
            action=webhook.action,
            base_sha=webhook.pull_request.base.sha,
            head_sha=webhook.pull_request.head.sha,
        )
    except ValidationError:
        raise HTTPException(422, "Invalid pull request payload") from None
    queue = ReviewJobQueue(request.app.state.redis, settings.review_job_ttl_seconds)
    try:
        async with asyncio.timeout(settings.webhook_enqueue_timeout_seconds):
            accepted = await queue.enqueue(job)
    except (RedisError, OSError, TimeoutError, ValueError) as error:
        logger.warning(
            "Webhook enqueue failed",
            extra={"stage": "enqueue", "error_type": type(error).__name__},
        )
        raise HTTPException(503, "Review queue unavailable; retry this delivery") from None
    logger.info(
        "Webhook job accepted",
        extra={
            "review_id": str(accepted.review_id),
            "repository": job.repository,
            "pr_number": job.pr_number,
            "stage": "enqueue",
            "status": accepted.status,
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        },
    )
    return accepted
