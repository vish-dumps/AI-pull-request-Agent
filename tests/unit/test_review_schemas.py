import pytest
from pydantic import ValidationError

from app.schemas.review import FindingInput, ReviewCompletion


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf")])
def test_finding_rejects_invalid_confidence(sample_result, confidence):
    finding = sample_result.findings[0].model_dump()
    with pytest.raises(ValidationError):
        FindingInput.model_validate({**finding, "confidence": confidence})


@pytest.mark.parametrize("risk_score", [-1, float("nan"), float("inf")])
def test_completion_rejects_invalid_risk_scores(sample_result, risk_score):
    with pytest.raises(ValidationError):
        ReviewCompletion.model_validate({**sample_result.model_dump(), "risk_score": risk_score})


@pytest.mark.parametrize("status", ["QUEUED", "PROCESSING", "FAILED"])
def test_completion_only_accepts_completed_or_partial_results(sample_result, status):
    with pytest.raises(ValidationError):
        ReviewCompletion.model_validate({**sample_result.model_dump(), "status": status})
