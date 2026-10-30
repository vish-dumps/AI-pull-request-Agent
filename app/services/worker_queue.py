"""Reliable receipts around the existing Redis list. All script keys are explicit.

Redis standalone is required. Queue/receipt/retry data survives metadata expiry;
only status/cache data has TTLs. PostgreSQL remains authoritative for final reviews.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import ValidationError
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.exceptions import LeaseLostError
from app.schemas.jobs import JobStatusRead, ReviewJob
from app.schemas.review import ReviewStatus
from app.services.job_queue import REVIEW_QUEUE_KEY

PROCESSING = "reviews:processing"
LEASES = "reviews:leases"
DELAYED = "reviews:delayed"
RETRY_JOBS = "reviews:retry_jobs"
DEAD = "reviews:dead"
TERMINAL = {ReviewStatus.COMPLETED, ReviewStatus.PARTIAL, ReviewStatus.FAILED}

NOW = "local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)\n"
OWNED = """
if redis.call('GET', KEYS[4]) ~= ARGV[1]
or redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
"""
# Keys: pending, processing, leases, review lease, job, status, metadata, attempts,
# delayed, retry jobs, dead. Each receipt operation uses the same key layout.
CLAIM = (
    NOW
    + """
if redis.call('LINDEX', KEYS[1], 0) ~= ARGV[2] then return 0 end
if redis.call('EXISTS', KEYS[4]) == 1 then
    redis.call('RPUSH', KEYS[1], redis.call('LPOP', KEYS[1])); return 0
end
redis.call('LPOP', KEYS[1])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('ZADD', KEYS[3], now+ARGV[3], ARGV[1])
redis.call('SET', KEYS[4], ARGV[1], 'PX', ARGV[3])
redis.call('SET', KEYS[5], ARGV[2], 'EX', ARGV[4])
redis.call('SET', KEYS[6], 'QUEUED', 'NX', 'EX', ARGV[4])
return 1
"""
)
RENEW = (
    OWNED
    + NOW
    + """
redis.call('PEXPIRE', KEYS[4], ARGV[3])
redis.call('ZADD', KEYS[3], now+ARGV[3], ARGV[1])
for i=5,7 do redis.call('EXPIRE', KEYS[i], ARGV[4]) end
return 1
"""
)
BEGIN = (
    OWNED
    + """
local attempts=redis.call('INCR', KEYS[8])
redis.call('SET', KEYS[6], 'PROCESSING', 'EX', ARGV[4])
redis.call('HSET', KEYS[7], 'attempts', attempts, 'error', '', 'retry_at', '')
redis.call('EXPIRE', KEYS[7], ARGV[4])
return attempts
"""
)
REMOVE_RECEIPT = """
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('DEL', KEYS[4])
"""
FINISH = (
    OWNED
    + REMOVE_RECEIPT
    + """
local attempts=redis.call('GET', KEYS[8]) or redis.call('HGET', KEYS[7], 'attempts') or '0'
redis.call('SET', KEYS[5], ARGV[2], 'EX', ARGV[4])
redis.call('SET', KEYS[6], ARGV[5], 'EX', ARGV[4])
redis.call('HSET', KEYS[7], 'attempts', attempts, 'error', ARGV[6], 'retry_at', '')
redis.call('EXPIRE', KEYS[7], ARGV[4])
redis.call('DEL', KEYS[8])
if ARGV[7] ~= '' then
    redis.call('LPUSH', KEYS[11], ARGV[7]); redis.call('LTRIM', KEYS[11], 0, ARGV[8]-1)
end
return 1
"""
)
DEFER = (
    OWNED
    + NOW
    + REMOVE_RECEIPT
    + """
redis.call('HSET', KEYS[10], ARGV[1], ARGV[2])
redis.call('ZADD', KEYS[9], now+ARGV[5], ARGV[1])
redis.call('SET', KEYS[5], ARGV[2], 'EX', ARGV[4])
redis.call('SET', KEYS[6], 'QUEUED', 'EX', ARGV[4])
redis.call('HSET', KEYS[7], 'attempts', redis.call('GET', KEYS[8]) or '0',
    'error', ARGV[6], 'retry_at', now+ARGV[5])
redis.call('EXPIRE', KEYS[7], ARGV[4])
return 1
"""
)
PROMOTE = (
    NOW
    + """
local due=redis.call('ZSCORE', KEYS[9], ARGV[1])
if not due or tonumber(due)>now then return 0 end
local raw=redis.call('HGET', KEYS[10], ARGV[1])
if raw then redis.call('RPUSH', KEYS[1], raw) end
redis.call('HDEL', KEYS[10], ARGV[1]); redis.call('ZREM', KEYS[9], ARGV[1])
return 1
"""
)
RECOVER = (
    NOW
    + """
local due=redis.call('ZSCORE', KEYS[3], ARGV[1])
if not due or tonumber(due)>now then return 0 end
if redis.call('GET', KEYS[4]) == ARGV[1] then
    redis.call('ZADD', KEYS[3], now+redis.call('PTTL', KEYS[4]), ARGV[1]); return 0
end
local raw=redis.call('HGET', KEYS[2], ARGV[1])
if raw then
    redis.call('RPUSH', KEYS[1], raw)
    local status=redis.call('GET', KEYS[6])
    if redis.call('EXISTS', KEYS[4]) == 0
    and status~='COMPLETED' and status~='PARTIAL' and status~='FAILED' then
        redis.call('SET', KEYS[5], raw, 'EX', ARGV[4])
        redis.call('SET', KEYS[6], 'QUEUED', 'EX', ARGV[4])
    end
end
redis.call('HDEL', KEYS[2], ARGV[1]); redis.call('ZREM', KEYS[3], ARGV[1])
return 1
"""
)
QUARANTINE = """
local kind=redis.call('TYPE',KEYS[2]).ok
if kind~='none' and kind~='list' then return redis.error_reply('Invalid dead-letter key type') end
if redis.call('LINDEX', KEYS[1], 0) ~= ARGV[1] then return 0 end
redis.call('LPOP', KEYS[1]); redis.call('LPUSH', KEYS[2], ARGV[2])
redis.call('LTRIM', KEYS[2], 0, ARGV[3]-1)
return 1
"""
CORRUPT_RECEIPT = (
    NOW
    + """
local kind=redis.call('TYPE',KEYS[3]).ok
if kind~='none' and kind~='list' then return redis.error_reply('Invalid dead-letter key type') end
local due=redis.call('ZSCORE',KEYS[2],ARGV[1])
if not due or tonumber(due)>now or redis.call('HGET',KEYS[1],ARGV[1])~=ARGV[2] then return 0 end
redis.call('HDEL',KEYS[1],ARGV[1]); redis.call('ZREM',KEYS[2],ARGV[1])
redis.call('LPUSH',KEYS[3],ARGV[3]); redis.call('LTRIM',KEYS[3],0,ARGV[4]-1)
return 1
"""
)
STATUS = """
local status=redis.call('GET', KEYS[1])
if not status then return {} end
return {status, redis.call('HGET', KEYS[2], 'attempts') or '0',
    redis.call('HGET', KEYS[2], 'error') or '', redis.call('HGET', KEYS[2], 'retry_at') or ''}
"""
TYPE_GUARD = """
local expected={'list','hash','zset','string','string','string',
    'hash','string','zset','hash','list'}
for i,key in ipairs(KEYS) do
    local kind=redis.call('TYPE',key).ok
    if kind ~= 'none' and kind ~= expected[i] then
        return redis.error_reply('Worker queue key type mismatch')
    end
end
"""
REJECT = (
    OWNED
    + REMOVE_RECEIPT
    + """
redis.call('LPUSH', KEYS[11], ARGV[5]); redis.call('LTRIM', KEYS[11], 0, ARGV[6]-1)
return 1
"""
)


@dataclass(frozen=True)
class Receipt:
    token: str
    raw: str
    job: ReviewJob


def decode(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value


class WorkerQueue:
    def __init__(self, redis: Redis, settings: Settings, *, namespace: str = "") -> None:
        self.redis, self.settings = redis, settings
        self.namespace = namespace

    def key(self, name: str) -> str:
        return self.namespace + name

    def keys(self, job: ReviewJob) -> list[str]:
        prefix = self.key(f"review:{job.review_id}:")
        return [
            self.key(REVIEW_QUEUE_KEY),
            self.key(PROCESSING),
            self.key(LEASES),
            prefix + "lease",
            prefix + "job",
            prefix + "status",
            prefix + "metadata",
            prefix + "attempts",
            self.key(DELAYED),
            self.key(RETRY_JOBS),
            self.key(DEAD),
        ]

    async def _eval(self, script: str, receipt: Receipt, *extra: object) -> int:
        keys = self.keys(receipt.job)
        return await self.redis.eval(
            TYPE_GUARD + script,
            len(keys),
            *keys,
            receipt.token,
            receipt.raw,
            int(self.settings.worker_lease_seconds * 1000),
            self.settings.review_job_ttl_seconds,
            *extra,
        )

    async def claim(self) -> Receipt | None:
        raw = await self.redis.lindex(self.key(REVIEW_QUEUE_KEY), 0)
        if raw is None:
            return None
        try:
            if len(raw) > 32768:
                raise ValueError("Queue envelope too large")
            body = decode(raw)
            job = ReviewJob.model_validate_json(body)
        except (ValueError, ValidationError, UnicodeError):
            digest = hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest()
            await self.redis.eval(
                QUARANTINE,
                2,
                self.key(REVIEW_QUEUE_KEY),
                self.key(DEAD),
                raw,
                json.dumps({"error": "Invalid queue envelope", "sha256": digest}),
                self.settings.worker_dead_letter_limit,
            )
            return None
        receipt = Receipt(str(uuid4()), body, job)
        return receipt if await self._eval(CLAIM, receipt) else None

    async def renew(self, receipt: Receipt) -> None:
        if not await self._eval(RENEW, receipt):
            raise LeaseLostError("Worker receipt is no longer owned")

    async def reject(self, receipt: Receipt) -> None:
        letter = json.dumps(
            {
                "job": receipt.job.model_dump(mode="json"),
                "error": "Queue identity conflicts with stored review",
            }
        )
        if not await self._eval(REJECT, receipt, letter, self.settings.worker_dead_letter_limit):
            raise LeaseLostError("Worker receipt is no longer owned")

    async def attempts(self, receipt: Receipt) -> int:
        return int(await self.redis.get(self.keys(receipt.job)[7]) or 0)

    async def begin(self, receipt: Receipt) -> int:
        result = await self._eval(BEGIN, receipt)
        if not result:
            raise LeaseLostError("Worker receipt is no longer owned")
        return result

    async def finish(
        self, receipt: Receipt, status: ReviewStatus, *, error: str = "", dead: bool = False
    ) -> None:
        if status not in TERMINAL:
            raise ValueError("Only a terminal outcome can be acknowledged")
        letter = (
            json.dumps({"job": receipt.job.model_dump(mode="json"), "error": error}) if dead else ""
        )
        result = await self._eval(
            FINISH, receipt, status.value, error, letter, self.settings.worker_dead_letter_limit
        )
        if not result:
            raise LeaseLostError("Worker receipt is no longer owned")

    async def defer(self, receipt: Receipt, delay: float, error: str = "") -> None:
        if not await self._eval(DEFER, receipt, int(delay * 1000), error):
            raise LeaseLostError("Worker receipt is no longer owned")

    async def maintain(self) -> int:
        seconds, micros = await self.redis.time()
        now = seconds * 1000 + micros // 1000
        count = 0
        for sorted_key, payload_key, script in (
            (DELAYED, RETRY_JOBS, PROMOTE),
            (LEASES, PROCESSING, RECOVER),
        ):
            sorted_key, payload_key = self.key(sorted_key), self.key(payload_key)
            tokens = await self.redis.zrangebyscore(
                sorted_key, "-inf", now, start=0, num=self.settings.worker_maintenance_batch
            )
            for token in tokens:
                raw = await self.redis.hget(payload_key, token)
                if raw is None:
                    await self.redis.zrem(sorted_key, token)
                    continue
                try:
                    receipt = Receipt(
                        decode(token), decode(raw), ReviewJob.model_validate_json(raw)
                    )
                except (ValueError, ValidationError, UnicodeError):
                    digest = hashlib.sha256(
                        raw.encode() if isinstance(raw, str) else raw
                    ).hexdigest()
                    count += await self.redis.eval(
                        CORRUPT_RECEIPT,
                        3,
                        payload_key,
                        sorted_key,
                        self.key(DEAD),
                        token,
                        raw,
                        json.dumps({"error": "Invalid durable queue envelope", "sha256": digest}),
                        self.settings.worker_dead_letter_limit,
                    )
                    continue
                count += await self._eval(script, receipt)
        return count

    async def status(self, review_id: UUID) -> JobStatusRead | None:
        values = await self.redis.eval(
            STATUS,
            2,
            self.key(f"review:{review_id}:status"),
            self.key(f"review:{review_id}:metadata"),
        )
        if not values:
            return None
        status, attempts, error, retry_at = map(decode, values)
        return JobStatusRead(
            review_id=review_id,
            status=status,
            attempts=int(attempts),
            error=error or None,
            next_retry_at=datetime.fromtimestamp(float(retry_at) / 1000, UTC) if retry_at else None,
        )
