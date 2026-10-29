from app.schemas.review import FindingInput
from app.services.aggregation_service import deduplicate_findings


def finding(**overrides):
    return FindingInput(
        agent_type="security",
        file_path="users.py",
        line_number=10,
        severity="HIGH",
        title="Unsafe query",
        description="Query uses user input.",
        suggestion="Use parameters.",
        confidence=0.8,
    ).model_copy(update=overrides)


def test_duplicate_titles_with_nearby_lines_keep_strongest_finding():
    first = finding(title="Unsafe QUERY!", line_number=10)
    strongest = finding(title="Unsafe query", line_number=12, confidence=0.95)
    assert deduplicate_findings([first, strongest]) == [strongest]


def test_separate_files_distant_lines_and_unknown_lines_are_not_collapsed():
    findings = [
        finding(),
        finding(file_path="other.py"),
        finding(line_number=50),
        finding(line_number=None),
    ]
    assert len(deduplicate_findings(findings)) == 4


def test_missing_line_duplicates_are_collapsed_and_priority_is_deterministic():
    high = finding(line_number=None)
    critical = finding(file_path="auth.py", severity="CRITICAL", confidence=0.9)
    result = deduplicate_findings([high, finding(line_number=None), critical])
    assert result == [critical, high]
