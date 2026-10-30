import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.base import prepare_review_input
from app.agents.factory import create_reviewers
from app.core.exceptions import EmbeddingError, WorkflowConflictError
from app.graph.workflow import ReviewWorkflow
from app.schemas.context import RetrievedChunk, RetrievedContext
from app.services.llm_service import MockLLMProvider


def snippet(context):
    return RetrievedChunk(
        repository=context.repository,
        commit_sha=context.base_sha,
        file_path="src/related.py",
        start_line=4,
        end_line=5,
        content="def related():\n    return 'supporting context'\n",
        similarity=0.9,
    )


def retriever(context):
    return SimpleNamespace(
        identity="test-retrieval-profile",
        retrieve=AsyncMock(
            return_value=RetrievedContext(source="pgvector", chunks=[snippet(context)])
        ),
    )


def test_all_reviewers_receive_same_bounded_revision_context(settings, multi_context, multi_job):
    provider = MockLLMProvider()
    provider.generate = AsyncMock(wraps=provider.generate)
    service = retriever(multi_context)

    async def run():
        workflow = ReviewWorkflow(
            reviewers=create_reviewers(provider, settings), mock_mode=True, context_service=service
        )
        state = await workflow.start(multi_job, multi_context)
        report = await workflow.report(multi_job.review_id)
        assert state["schema_version"] == 3
        assert report.context_source == "pgvector"
        assert report.status == "COMPLETED"
        assert len(report.retrieved_context) == 1
        assert "embedding" not in json.dumps(state)
        assert "system_prompt" not in report.model_dump_json()

    asyncio.run(run())
    service.retrieve.assert_awaited_once()
    payloads = [json.loads(call.args[0].user_prompt) for call in provider.generate.await_args_list]
    assert len(payloads) == 4
    assert all(
        payload["retrieved_context"] == payloads[0]["retrieved_context"] for payload in payloads
    )


@pytest.mark.parametrize("failure", [EmbeddingError("private-response"), TimeoutError()])
def test_context_failure_preserves_review_with_safe_partial_warning(
    settings, multi_context, multi_job, failure
):
    service = retriever(multi_context)
    service.retrieve.side_effect = failure

    async def run():
        workflow = ReviewWorkflow(
            reviewers=create_reviewers(MockLLMProvider(), settings),
            mock_mode=True,
            context_service=service,
        )
        await workflow.start(multi_job, multi_context)
        report = await workflow.report(multi_job.review_id)
        assert report.status == "PARTIAL"
        assert len(report.review.findings) == 4
        assert report.context_source == "diff_only"
        assert "private-response" not in report.model_dump_json()
        assert "unavailable" in report.context_warnings[0]

    asyncio.run(run())


def test_context_checkpoint_reuses_snippets_and_rejects_changed_mode(
    settings, multi_context, multi_job
):
    provider = MockLLMProvider()
    provider.generate = AsyncMock(wraps=provider.generate)
    service = retriever(multi_context)
    saver = InMemorySaver()

    async def run():
        first = ReviewWorkflow(
            reviewers=create_reviewers(provider, settings),
            mock_mode=True,
            context_service=service,
            checkpointer=saver,
            stop_before_persist=True,
        )
        await first.start(multi_job, multi_context)
        for context_service in (None, SimpleNamespace(identity="changed-profile")):
            wrong = ReviewWorkflow(
                reviewers=create_reviewers(provider, settings),
                mock_mode=True,
                context_service=context_service,
                checkpointer=saver,
            )
            with pytest.raises(WorkflowConflictError):
                await wrong.resume(multi_job.review_id)
        service.retrieve.side_effect = AssertionError("Do not retrieve again")
        second = ReviewWorkflow(
            reviewers=create_reviewers(provider, settings),
            mock_mode=True,
            context_service=service,
            checkpointer=saver,
        )
        await second.resume(multi_job.review_id)
        assert (await second.report(multi_job.review_id)).status == "COMPLETED"

    asyncio.run(run())
    assert provider.generate.await_count == 4
    assert service.retrieve.await_count == 1


def test_retrieved_context_cannot_create_findings_outside_diff(settings, multi_context):
    class UntrustedProvider:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            return json.dumps(
                {
                    "findings": [
                        {
                            "agent_type": "security",
                            "file_path": "src/related.py",
                            "line_number": 4,
                            "evidence": "def related():",
                            "severity": "LOW",
                            "title": "Invented issue",
                            "description": "Existing code",
                            "suggestion": "Change it",
                            "confidence": 0.9,
                        }
                    ],
                    "summary": "Existing code issue",
                    "risk_score": 1,
                }
            )

    provider = UntrustedProvider()
    from app.agents.security_agent import SecurityAgent

    result = asyncio.run(
        SecurityAgent(provider, settings).review(
            multi_context, retrieved_context=[snippet(multi_context)]
        )
    )
    assert result.status == "FAILED"
    assert provider.calls == 2


def test_prompt_context_budget_and_revision_guard(settings, multi_context):
    configured = settings.model_copy(update={"max_context_chars": 128, "max_context_chunks": 1})
    chunk = snippet(multi_context).model_copy(update={"content": "x" * 1000})
    payload, _, _ = prepare_review_input(multi_context, configured, [chunk, chunk])
    assert len(payload["retrieved_context"]) == 1
    assert len(payload["retrieved_context"][0]["content"]) == 128
    with pytest.raises(ValueError, match="base snapshot"):
        prepare_review_input(
            multi_context, settings, [chunk.model_copy(update={"commit_sha": "b" * 40})]
        )
