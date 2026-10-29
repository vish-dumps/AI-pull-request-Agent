import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import SecretStr

from app.agents.security_agent import SecurityAgent
from app.core.exceptions import ReviewConflictError, WorkflowConflictError, WorkflowPersistenceError
from app.graph.nodes import ReviewNodes
from app.graph.workflow import ReviewWorkflow
from app.services.llm_service import MockLLMProvider

STAGES = ["ingest", "context", "security", "aggregate", "persist"]


def saver():
    return InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=[]))


def test_workflow_runs_all_stages_with_only_json_state(settings, security_job, security_context):
    configured = settings.model_copy(update={"llm_api_key": SecretStr("private-llm-key")})

    async def run():
        workflow = ReviewWorkflow(SecurityAgent(MockLLMProvider(), configured), mock_mode=True)
        state = await workflow.start(security_job, security_context)
        report = await workflow.report(security_job.review_id)
        assert state["completed_stages"] == STAGES
        assert state["context_source"] == "diff_only"
        assert state["retrieved_context"] == []
        assert report.status == "PARTIAL"
        assert report.review.risk_score == 4.5
        assert not report.persisted
        assert not report.paused
        assert "private-llm-key" not in json.dumps(state)
        assert "system_prompt" not in report.model_dump_json()

    asyncio.run(run())


def test_static_checkpoint_resume_skips_completed_agent(settings, security_job, security_context):
    provider = MockLLMProvider()
    provider.generate = AsyncMock(wraps=provider.generate)
    checkpointer = saver()

    async def run():
        first = ReviewWorkflow(
            SecurityAgent(provider, settings),
            mock_mode=True,
            checkpointer=checkpointer,
            stop_before_persist=True,
        )
        await first.start(security_job, security_context)
        paused = await first.report(security_job.review_id)
        assert paused.paused
        assert paused.pending_nodes == ["persist"]
        assert paused.status == "PROCESSING"
        assert paused.completed_stages == STAGES[:-1]
        second = ReviewWorkflow(
            SecurityAgent(provider, settings), mock_mode=True, checkpointer=checkpointer
        )
        await second.resume(security_job.review_id)
        done = await second.report(security_job.review_id)
        assert done.completed_stages == STAGES
        assert not done.paused
        provider.generate.assert_awaited_once()

    asyncio.run(run())


def test_failed_node_resumes_without_repeating_security(
    settings, security_job, security_context, monkeypatch
):
    original = ReviewNodes.persist
    calls = 0

    async def fail_once(self, state):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkflowPersistenceError("Simulated persistence failure")
        return await original(self, state)

    monkeypatch.setattr(ReviewNodes, "persist", fail_once)
    provider = MockLLMProvider()
    provider.generate = AsyncMock(wraps=provider.generate)

    async def run():
        workflow = ReviewWorkflow(SecurityAgent(provider, settings), mock_mode=True)
        with pytest.raises(WorkflowPersistenceError):
            await workflow.start(security_job, security_context)
        pending = await workflow.report(security_job.review_id)
        assert pending.pending_nodes == ["persist"]
        await workflow.resume(security_job.review_id)
        assert (await workflow.report(security_job.review_id)).completed_stages == STAGES
        provider.generate.assert_awaited_once()

    asyncio.run(run())


def test_unexpected_agent_failure_becomes_failed_graph_data(
    settings, security_job, security_context, monkeypatch
):
    agent = SecurityAgent(MockLLMProvider(), settings)
    monkeypatch.setattr(agent, "review", AsyncMock(side_effect=RuntimeError("private-code")))

    async def run():
        workflow = ReviewWorkflow(agent, mock_mode=True)
        result = await workflow.start(security_job, security_context)
        assert result["status"] == "FAILED"
        assert result["completed_stages"] == STAGES
        assert result["aggregated_review"] is None
        assert result["errors"] == [
            {"stage": "security", "message": "Security agent failed unexpectedly"}
        ]
        assert "private-code" not in json.dumps(result)

    asyncio.run(run())


def test_cancelled_agent_can_resume(settings, security_job, security_context, monkeypatch):
    agent = SecurityAgent(MockLLMProvider(), settings)
    real_review = agent.review

    async def run():
        entered = asyncio.Event()

        async def stalled(context):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(agent, "review", stalled)
        workflow = ReviewWorkflow(agent, mock_mode=True)
        task = asyncio.create_task(workflow.start(security_job, security_context))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await workflow.report(security_job.review_id)).pending_nodes == ["security"]
        monkeypatch.setattr(agent, "review", real_review)
        result = await workflow.resume(security_job.review_id)
        assert result["completed_stages"] == STAGES

    asyncio.run(run())


def test_completed_thread_is_reused_without_reexecution(settings, security_job, security_context):
    provider = MockLLMProvider()
    provider.generate = AsyncMock(wraps=provider.generate)

    async def run():
        workflow = ReviewWorkflow(SecurityAgent(provider, settings), mock_mode=True)
        await workflow.start(security_job, security_context)
        second = await workflow.start(security_job, security_context)
        assert second["reused"]
        assert (await workflow.report(security_job.review_id)).reused
        assert (await workflow.resume(security_job.review_id))["reused"]
        provider.generate.assert_awaited_once()

    asyncio.run(run())


def test_start_cannot_overwrite_a_pending_checkpoint(settings, security_job, security_context):
    async def run():
        workflow = ReviewWorkflow(
            SecurityAgent(MockLLMProvider(), settings), mock_mode=True, stop_before_persist=True
        )
        await workflow.start(security_job, security_context)
        with pytest.raises(WorkflowConflictError, match="resume"):
            await workflow.start(security_job, security_context)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["mock_mode", "persist", "context", "job"])
def test_checkpoint_reuse_rejects_incompatible_inputs(
    settings, security_job, security_context, change
):
    checkpointer = saver()

    async def run():
        first = ReviewWorkflow(
            SecurityAgent(MockLLMProvider(), settings), mock_mode=True, checkpointer=checkpointer
        )
        await first.start(security_job, security_context)
        second = ReviewWorkflow(
            SecurityAgent(MockLLMProvider(), settings),
            mock_mode=change != "mock_mode",
            checkpointer=checkpointer,
        )
        if change == "persist":
            second.persist = True
        context = (
            security_context.model_copy(update={"description": "different"})
            if change == "context"
            else security_context
        )
        job = (
            security_job.model_copy(update={"head_sha": "c" * 40})
            if change == "job"
            else security_job
        )
        with pytest.raises(WorkflowConflictError):
            await second.start(job, context)

    asyncio.run(run())


def test_unknown_resume_and_mismatched_revision_fail(settings, security_job, security_context):
    async def run():
        workflow = ReviewWorkflow(SecurityAgent(MockLLMProvider(), settings), mock_mode=True)
        with pytest.raises(LookupError):
            await workflow.resume(security_job.review_id)
        with pytest.raises(ReviewConflictError):
            await workflow.start(
                security_job, security_context.model_copy(update={"head_sha": "c" * 40})
            )

    asyncio.run(run())


@pytest.mark.parametrize("change", ["schema_version", "review_id"])
def test_checkpoint_version_and_thread_binding_are_checked(
    settings, security_job, security_context, change
):
    async def run():
        workflow = ReviewWorkflow(SecurityAgent(MockLLMProvider(), settings), mock_mode=True)
        await workflow.start(security_job, security_context)
        await workflow.graph.aupdate_state(
            workflow.config(security_job.review_id),
            {change: 999 if change == "schema_version" else str(uuid4())},
            as_node="persist",
        )
        with pytest.raises(WorkflowConflictError):
            await workflow.resume(security_job.review_id)

    asyncio.run(run())
