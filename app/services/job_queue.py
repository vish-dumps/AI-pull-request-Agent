from uuid import UUID

from redis.asyncio import Redis

from app.schemas.jobs import ReviewJob, WebhookAccepted
from app.schemas.review import ReviewStatus

REVIEW_QUEUE_KEY = "reviews:pending"

# A single script makes acceptance and delivery deduplication atomic. The queue
# contains the whole small job, so expiring metadata cannot lose a queued job.
ENQUEUE_JOB_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing then
    return existing
end
redis.call('RPUSH', KEYS[4], ARGV[2])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('SET', KEYS[3], 'QUEUED', 'EX', ARGV[3])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
return ARGV[1]
"""


class ReviewJobQueue:
    def __init__(self, redis: Redis, ttl_seconds: int, *, namespace: str = "") -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.namespace = namespace

    async def enqueue(self, job: ReviewJob) -> WebhookAccepted:
        review_id = await self.redis.eval(
            ENQUEUE_JOB_SCRIPT,
            4,
            self.namespace + f"github:delivery:{job.delivery_id}",
            self.namespace + f"review:{job.review_id}:job",
            self.namespace + f"review:{job.review_id}:status",
            self.namespace + REVIEW_QUEUE_KEY,
            str(job.review_id),
            job.model_dump_json(),
            self.ttl_seconds,
        )
        accepted_id = UUID(review_id.decode() if isinstance(review_id, bytes) else review_id)
        duplicate = accepted_id != job.review_id
        status = ReviewStatus.QUEUED
        if duplicate:
            current = await self.redis.get(self.namespace + f"review:{accepted_id}:status")
            if current is not None:
                status = ReviewStatus(current.decode() if isinstance(current, bytes) else current)
        return WebhookAccepted(review_id=accepted_id, duplicate=duplicate, status=status)
