import pytest

from app.schemas.agents import SecurityAgentResult, SecurityReview
from app.services.review_service import security_completion


@pytest.mark.parametrize(
    "severity,confidence,score,risk,recommendation",
    [
        ("LOW", 0.5, 0.5, "LOW", "COMMENT"),
        ("MEDIUM", 1, 2, "MEDIUM", "COMMENT"),
        ("HIGH", 0.9, 4.5, "HIGH", "REQUEST_CHANGES"),
        ("CRITICAL", 0.9, 9, "CRITICAL", "REQUEST_CHANGES"),
    ],
)
def test_security_risk_is_deterministic(
    security_output, severity, confidence, score, risk, recommendation
):
    security_output["risk_score"] = 0  # Deliberately contradict the model's advisory score.
    security_output["findings"][0].update(severity=severity, confidence=confidence)
    result = SecurityAgentResult(
        status="COMPLETED",
        review=SecurityReview.model_validate(security_output),
        attempts=1,
        duration_ms=1,
    )
    completion = security_completion(result, mock_mode=False)
    assert completion.risk_score == score
    assert completion.risk == risk
    assert completion.recommendation == recommendation
    assert completion.status == "PARTIAL"


def test_zero_security_findings_does_not_approve_a_full_review():
    result = SecurityAgentResult(
        status="COMPLETED",
        review=SecurityReview(
            findings=[], summary="No supported security findings.", risk_score=100
        ),
        attempts=1,
        duration_ms=1,
    )
    completion = security_completion(result, mock_mode=True)
    assert completion.risk_score == 0
    assert completion.status == "PARTIAL"
    assert completion.recommendation == "COMMENT"
    assert completion.summary.startswith("[MOCK]")
