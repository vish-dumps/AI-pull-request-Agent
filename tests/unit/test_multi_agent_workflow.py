import asyncio
import json
from collections import Counter
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.factory import create_reviewers
from app.core.exceptions import WorkflowConflictError
from app.graph.workflow import ReviewWorkflow
from app.schemas.review import AgentType
from app.services.llm_service import MockLLMProvider


def test_all_four_provider_requests_start_before_any_completes(settings, multi_job, multi_context):
    class BarrierProvider(MockLLMProvider):
        def __init__(self):
            self.entered = set()
            self.all_started = asyncio.Event()

        async def generate(self, request):
            self.entered.add(request.schema_name)
            if len(self.entered) == 4:
                self.all_started.set()
            await asyncio.wait_for(self.all_started.wait(), timeout=2)
            return await super().generate(request)

    async def run():
        provider = BarrierProvider()
        workflow = ReviewWorkflow(reviewers=create_reviewers(provider, settings), mock_mode=True)
        state = await workflow.start(multi_job, multi_context)
        assert len(provider.entered) == 4
        assert state["status"] == "COMPLETED"
        assert set(state["agent_results"]) == {kind.value for kind in AgentType}
        stages = state["completed_stages"]
        assert stages[:2] == ["ingest", "context"]
        assert set(stages[2:6]) == {kind.value for kind in AgentType}
        assert stages[6:] == ["aggregate", "persist"]
        report = await workflow.report(multi_job.review_id)
        assert len(report.review.findings) == 4
        assert report.review.risk_score == 7.7

    asyncio.run(run())


def test_stalled_reviewer_times_out_without_discarding_other_results(
    settings, multi_job, multi_context
):
    async def run():
        configured = settings.model_copy(update={"agent_timeout_seconds": 0.05})
        reviewers = create_reviewers(MockLLMProvider(), configured)

        async def stalled(context):
            await asyncio.Event().wait()

        reviewers[AgentType.DOCUMENTATION].review = stalled
        workflow = ReviewWorkflow(reviewers=reviewers, mock_mode=True)
        state = await workflow.start(multi_job, multi_context)
        assert state["status"] == "PARTIAL"
        assert len(state["aggregated_review"]["findings"]) == 3
        assert state["errors"] == [
            {"stage": "documentation", "message": "Documentation agent exceeded its deadline"}
        ]

    asyncio.run(run())


@pytest.mark.parametrize("failed_kind", list(AgentType))
def test_one_failed_reviewer_keeps_other_three_results(
    settings, multi_job, multi_context, failed_kind
):
    async def run():
        reviewers = create_reviewers(MockLLMProvider(), settings)
        reviewers[failed_kind].review = AsyncMock(side_effect=RuntimeError("private-error-data"))
        workflow = ReviewWorkflow(reviewers=reviewers, mock_mode=True)
        state = await workflow.start(multi_job, multi_context)
        assert state["status"] == "PARTIAL"
        assert len(state["aggregated_review"]["findings"]) == 3
        assert state["agent_results"][failed_kind]["status"] == "FAILED"
        assert state["errors"] == [
            {
                "stage": failed_kind.value,
                "message": f"{failed_kind.value.capitalize()} agent failed unexpectedly",
            }
        ]
        assert "private-error-data" not in json.dumps(state)
        assert "Incomplete reviewer categories" in state["aggregated_review"]["summary"]

    asyncio.run(run())


def test_all_failed_reviewers_produce_failed_not_clean_review(settings, multi_job, multi_context):
    async def run():
        reviewers = create_reviewers(MockLLMProvider(), settings)
        for reviewer in reviewers.values():
            reviewer.review = AsyncMock(side_effect=RuntimeError("not exposed"))
        workflow = ReviewWorkflow(reviewers=reviewers, mock_mode=True)
        state = await workflow.start(multi_job, multi_context)
        assert state["status"] == "FAILED"
        assert state["aggregated_review"] is None
        assert len(state["errors"]) == 4
        assert len(state["agent_results"]) == 4

    asyncio.run(run())


def test_invalid_result_object_is_isolated_to_its_reviewer(settings, multi_job, multi_context):
    async def run():
        reviewers = create_reviewers(MockLLMProvider(), settings)
        reviewers[AgentType.QUALITY].review = AsyncMock(return_value=None)
        state = await ReviewWorkflow(reviewers=reviewers, mock_mode=True).start(
            multi_job, multi_context
        )
        assert state["status"] == "PARTIAL"
        assert len(state["aggregated_review"]["findings"]) == 3
        assert state["agent_results"]["quality"]["status"] == "FAILED"

    asyncio.run(run())


def test_resume_after_cancelled_branch_reuses_successful_siblings(
    settings, multi_job, multi_context
):
    async def run():
        reviewers = create_reviewers(MockLLMProvider(), settings)
        finished = {kind: asyncio.Event() for kind in AgentType if kind != AgentType.QUALITY}
        blocked = asyncio.Event()
        counts = Counter()
        for kind, reviewer in reviewers.items():
            original = reviewer.review

            async def review(context, kind=kind, original=original):
                counts[kind] += 1
                if kind == AgentType.QUALITY and counts[kind] == 1:
                    blocked.set()
                    await asyncio.Event().wait()
                result = await original(context)
                if kind in finished:
                    finished[kind].set()
                return result

            reviewer.review = review
        saver = InMemorySaver()
        first = ReviewWorkflow(reviewers=reviewers, mock_mode=True, checkpointer=saver)
        task = asyncio.create_task(first.start(multi_job, multi_context))
        await asyncio.wait_for(
            asyncio.gather(blocked.wait(), *(event.wait() for event in finished.values())), 2
        )
        # Wait for actual pending checkpoint writes, not a timing assumption.
        async with asyncio.timeout(2):
            while True:
                checkpoint = await saver.aget_tuple(first.config(multi_job.review_id))
                written = {
                    kind
                    for _, channel, value in checkpoint.pending_writes
                    if channel == "agent_results"
                    for kind in value
                }
                if written >= {kind.value for kind in finished}:
                    break
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        second = ReviewWorkflow(reviewers=reviewers, mock_mode=True, checkpointer=saver)
        state = await second.resume(multi_job.review_id)
        assert state["status"] == "COMPLETED"
        assert counts[AgentType.QUALITY] == 2
        assert all(counts[kind] == 1 for kind in finished)
        assert len(state["completed_stages"]) == 8
        assert len(state["aggregated_review"]["findings"]) == 4

    asyncio.run(run())


def test_security_only_checkpoint_is_not_reinterpreted_as_full_review(
    settings, multi_job, multi_context
):
    async def run():
        reviewers = create_reviewers(MockLLMProvider(), settings)
        saver = InMemorySaver()
        old = ReviewWorkflow(
            reviewers[AgentType.SECURITY],
            mock_mode=True,
            checkpointer=saver,
            stop_before_persist=True,
        )
        await old.start(multi_job, multi_context)
        new = ReviewWorkflow(reviewers=reviewers, mock_mode=True, checkpointer=saver)
        with pytest.raises(WorkflowConflictError, match="version"):
            await new.resume(multi_job.review_id)
        await old.resume(multi_job.review_id)
        assert (await old.report(multi_job.review_id)).status == "PARTIAL"

    asyncio.run(run())
