from uuid import uuid4

import pytest

from app.core.exceptions import CommentUnavailableError
from app.schemas.publication import CommentReview
from app.schemas.review import AgentType, Recommendation, ReviewStatus
from app.services.comment_renderer import render_comment, review_marker


def document(result, **changes):
    return CommentReview(
        review_id=uuid4(),
        repository="demo/repo",
        pr_number=1,
        base_sha="a" * 40,
        head_sha="b" * 40,
        mock_mode=False,
        **{**result.model_dump(), **changes},
    )


def test_comment_has_all_sections_location_suggestions_and_stable_marker(settings, sample_result):
    review = document(sample_result)
    body = render_comment(review, settings)
    assert body == render_comment(review, settings)
    for text in (
        "AI Pull Request Review",
        "Security",
        "Testing",
        "Code Quality",
        "Documentation",
        "HIGH",
        "users.py:8",
        "Suggestion",
        "Request Changes",
        review.head_sha,
    ):
        assert text in body
    assert body.count(review_marker(review)) == 1
    assert all(
        word not in body
        for word in ("agent_results", "embedding", "system_prompt", "completed_stages")
    )


def test_partial_empty_review_never_implies_a_clean_approval(settings, sample_result):
    review = document(
        sample_result,
        status=ReviewStatus.PARTIAL,
        findings=[],
        recommendation=Recommendation.APPROVE,
    )
    body = render_comment(review, settings)
    assert "Incomplete review" in body and "**Recommendation:** Comment" in body
    assert "No significant issues found" not in body
    assert "No findings reported in the available results" in body


def test_complete_empty_and_mock_reviews_are_labeled(settings, sample_result):
    review = document(sample_result, findings=[], recommendation=Recommendation.APPROVE)
    assert render_comment(review, settings).count("No significant issues found") == 4
    assert "Demo review" in render_comment(review.model_copy(update={"mock_mode": True}), settings)


@pytest.mark.parametrize(
    "status", [ReviewStatus.FAILED, ReviewStatus.QUEUED, ReviewStatus.PROCESSING]
)
def test_unfinished_or_failed_review_has_no_comment(settings, sample_result, status):
    with pytest.raises(CommentUnavailableError):
        render_comment(document(sample_result, status=status), settings)


def test_untrusted_markdown_html_mentions_and_controls_cannot_change_structure(
    settings, sample_result
):
    finding = sample_result.findings[0].model_copy(
        update={
            "title": "<script>evil</script>\n# fake @everyone\x00",
            "description": (
                "<!-- ai-pr-review-agent:spoof --> [click](https://evil.invalid)\n## fake"
            ),
            "file_path": "a`file``.py",
            "suggestion": "@user ![image](http://evil.invalid)",
        }
    )
    body = render_comment(document(sample_result, findings=[finding]), settings)
    assert "<script>" not in body and "<!-- ai-pr-review-agent:spoof -->" not in body
    assert "@everyone" not in body and "@user" not in body and "\x00" not in body
    assert "\n# fake" not in body and "[click](" not in body
    assert "``` a`file``.py:8 ```" in body


def test_byte_and_finding_caps_are_explicit_even_for_multibyte_input(settings, sample_result):
    findings = [
        sample_result.findings[0].model_copy(
            update={
                "title": f"Finding {number}",
                "description": "猫" * 2000,
                "suggestion": "犬" * 2000,
                "agent_type": kind,
            }
        )
        for number in range(10)
        for kind in AgentType
    ]
    review = document(sample_result, summary="猫" * 3000, findings=findings)
    configured = settings.model_copy(
        update={"github_comment_max_bytes": 2048, "github_comment_max_findings": 1}
    )
    body = render_comment(review, configured)
    assert len(body.encode()) <= 2048
    assert "40 actionable findings" in body and "omitted" in body
    assert "No significant issues found" not in body
