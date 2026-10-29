import asyncio
import json

import httpx
from fakeredis.aioredis import FakeRedis

from app.schemas.jobs import ReviewJob
from app.services.github_service import GitHubService, build_github_client
from app.services.job_queue import REVIEW_QUEUE_KEY, ReviewJobQueue


def test_queued_job_can_be_ingested_to_normalized_context(settings, sample_job, sample_payload):
    """Exercise the queue/ingestion boundary without a worker, GitHub, or an LLM."""
    requests = []

    def github(request):
        requests.append(request)
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "users.py",
                        "status": "modified",
                        "additions": 8,
                        "deletions": 1,
                        "patch": "@@ -1 +1 @@\n-old\n+new",
                    },
                    {
                        "filename": "tests/test_users.py",
                        "status": "added",
                        "additions": 0,
                        "deletions": 0,
                        "patch": "@@ -0,0 +1 @@\n+# test",
                    },
                ],
            )
        return httpx.Response(200, json=sample_payload["pull_request"])

    async def run():
        async with FakeRedis(decode_responses=True) as redis:
            accepted = await ReviewJobQueue(redis, 60).enqueue(sample_job)
            job = ReviewJob.model_validate_json(await redis.lpop(REVIEW_QUEUE_KEY))
            assert job.review_id == accepted.review_id
            async with build_github_client(settings, httpx.MockTransport(github)) as client:
                context = await GitHubService(client, settings).get_pull_request_context(
                    job.repository,
                    job.pr_number,
                    expected_head_sha=job.head_sha,
                    expected_base_sha=job.base_sha,
                )
        assert context.repository == sample_job.repository
        assert context.head_sha == sample_job.head_sha
        assert len(context.changed_files) == context.total_changed_files == 2
        assert not context.files_truncated
        assert context.warnings == []
        # The object is JSON-safe for future persistence/checkpointing.
        assert json.loads(context.model_dump_json())["changed_files"][0]["filename"] == "users.py"

    asyncio.run(run())
    assert len(requests) == 3
