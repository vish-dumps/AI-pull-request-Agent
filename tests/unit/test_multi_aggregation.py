import asyncio

from app.agents.factory import create_reviewers
from app.schemas.review import AgentType
from app.services.aggregation_service import aggregate_reviews, deduplicate_findings
from app.services.llm_service import MockLLMProvider


def results(settings, context):
    async def run():
        reviewers = create_reviewers(MockLLMProvider(), settings)
        outcomes = await asyncio.gather(
            *(reviewer.review(context) for reviewer in reviewers.values())
        )
        return {outcome.agent: outcome for outcome in outcomes}

    return asyncio.run(run())


def test_all_success_zero_findings_can_approve_full_review(settings, sample_context):
    review = aggregate_reviews(results(settings, sample_context), tuple(AgentType), mock_mode=True)
    assert review.status == "COMPLETED"
    assert review.findings == []
    assert review.risk_score == 0
    assert review.recommendation == "APPROVE"


def test_missing_reviewer_never_approves_even_with_zero_findings(settings, sample_context):
    outcomes = results(settings, sample_context)
    del outcomes[AgentType.SECURITY]
    review = aggregate_reviews(outcomes, tuple(AgentType), mock_mode=False)
    assert review.status == "PARTIAL"
    assert review.recommendation == "COMMENT"
    assert "security" in review.summary


def test_coverage_warning_keeps_otherwise_complete_review_partial(settings, sample_context):
    outcomes = results(settings, sample_context)
    outcomes[AgentType.QUALITY].coverage_warnings.append("A changed patch was omitted")
    review = aggregate_reviews(outcomes, tuple(AgentType), mock_mode=False)
    assert review.status == "PARTIAL"
    assert review.recommendation == "COMMENT"


def test_no_successful_results_produces_no_fabricated_review():
    assert aggregate_reviews({}, tuple(AgentType), mock_mode=True) is None


def test_deduplication_preserves_distinct_unicode_titles(sample_result):
    finding = sample_result.findings[0]
    first = finding.model_copy(update={"title": "查询验证"})
    second = finding.model_copy(update={"title": "权限检查"})
    assert len(deduplicate_findings([first, second])) == 2


def test_advisory_scores_do_not_control_multi_agent_risk(settings, multi_context):
    outcomes = results(settings, multi_context)
    for result in outcomes.values():
        result.review.risk_score = 0
    review = aggregate_reviews(outcomes, tuple(AgentType), mock_mode=False)
    assert review.risk_score == 7.7
    assert review.risk == "HIGH"
    assert review.recommendation == "REQUEST_CHANGES"


def test_cross_category_duplicates_are_removed_before_scoring(settings, multi_context):
    outcomes = results(settings, multi_context)
    first = outcomes[AgentType.SECURITY].review.findings[0]
    other = outcomes[AgentType.QUALITY].review.findings[0]
    other.file_path = first.file_path
    other.title = first.title
    other.line_number = first.line_number
    review = aggregate_reviews(outcomes, tuple(AgentType), mock_mode=False)
    assert len(review.findings) == 3
    assert review.risk_score == 6.1
    assert sum(finding.title == first.title for finding in review.findings) == 1
