import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.agents.factory import create_reviewers
from app.schemas.review import AgentType
from app.services.llm_service import LLMProvider, MockLLMProvider


def test_prompts_have_distinct_responsibilities(settings):
    reviewers = create_reviewers(MockLLMProvider(), settings)
    assert len({reviewer.system_prompt for reviewer in reviewers.values()}) == 4
    assert "SQL/command" in reviewers[AgentType.SECURITY].system_prompt
    assert "unnecessary large allocations" in reviewers[AgentType.QUALITY].system_prompt
    assert "exact input/state" in reviewers[AgentType.TESTING].system_prompt
    assert "configuration defaults/types" in reviewers[AgentType.DOCUMENTATION].system_prompt
    assert all("UNTRUSTED DATA" in reviewer.system_prompt for reviewer in reviewers.values())


@pytest.mark.parametrize("kind", list(AgentType))
def test_each_specialist_returns_its_own_validated_finding(settings, multi_context, kind):
    reviewer = create_reviewers(MockLLMProvider(), settings)[kind]
    result = asyncio.run(reviewer.review(multi_context))
    assert result.agent == kind
    assert result.status == "COMPLETED"
    assert len(result.review.findings) == 1
    finding = result.review.findings[0]
    assert finding.agent_type == kind
    assert finding.evidence
    assert "Mock mode" in result.review.summary
    schema = reviewer.review_schema.model_json_schema()
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("kind", list(AgentType))
def test_each_specialist_rejects_cross_category_findings(settings, multi_context, kind):
    reviewer = create_reviewers(MockLLMProvider(), settings)[kind]
    valid = asyncio.run(reviewer.review(multi_context)).review.model_dump(mode="json")
    wrong = next(candidate for candidate in AgentType if candidate != kind)
    valid["findings"][0]["agent_type"] = wrong.value
    with pytest.raises(ValidationError):
        reviewer.review_schema.model_validate(valid)


@pytest.mark.parametrize("kind", list(AgentType))
def test_specialists_allow_zero_findings_and_skip_absent_patches(settings, multi_context, kind):
    for file in multi_context.changed_files:
        file.patch = None
    provider = Mock(spec=LLMProvider)
    provider.generate = AsyncMock()
    result = asyncio.run(create_reviewers(provider, settings)[kind].review(multi_context))
    assert result.status == "COMPLETED"
    assert result.review.findings == []
    assert result.coverage_warnings
    provider.generate.assert_not_awaited()


@pytest.mark.parametrize("kind", list(AgentType))
def test_output_repair_is_bounded_for_every_specialist(settings, multi_context, kind):
    provider = Mock(spec=LLMProvider)
    provider.generate = AsyncMock(return_value="not-json")
    result = asyncio.run(create_reviewers(provider, settings)[kind].review(multi_context))
    assert result.status == "FAILED"
    assert result.attempts == 2
    assert provider.generate.await_count == 2


def test_testing_and_documentation_suggestions_are_specific(settings, multi_context):
    async def run():
        reviewers = create_reviewers(MockLLMProvider(), settings)
        coverage = await reviewers[AgentType.TESTING].review(multi_context)
        docs = await reviewers[AgentType.DOCUMENTATION].review(multi_context)
        assert "load_user('')" in coverage.review.findings[0].suggestion
        assert "ValueError" in coverage.review.findings[0].suggestion
        assert "30" in docs.review.findings[0].suggestion
        assert "REVIEW_TIMEOUT" in docs.review.findings[0].evidence

    asyncio.run(run())
