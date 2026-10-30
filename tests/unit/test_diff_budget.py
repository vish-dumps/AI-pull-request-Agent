import asyncio

from app.agents.base import added_line_numbers, prepare_review_input
from app.agents.diff_budget import bounded_patch
from app.agents.factory import create_reviewers
from app.graph.workflow import ReviewWorkflow
from app.schemas.github import ChangedFile
from app.services.llm_service import MockLLMProvider


def changed(filename, patch="@@ -1 +1 @@\n-old\n+new"):
    return ChangedFile(filename=filename, status="modified", additions=1, deletions=1, patch=patch)


def test_limited_input_prioritizes_security_then_source_before_docs(settings, multi_context):
    context = multi_context.model_copy(
        update={
            "changed_files": [
                changed("README.md"),
                changed("dist/app.min.js"),
                changed("tests/test_users.py"),
                changed("src/users.py"),
                changed("src/auth/permissions.py"),
            ],
            "total_changed_files": 5,
        }
    )
    settings = settings.model_copy(update={"max_files_per_review": 2})
    payload, patches, warnings = prepare_review_input(context, settings)
    assert list(patches) == ["src/auth/permissions.py", "src/users.py"]
    assert [file["filename"] for file in payload["changed_files"]] == list(patches)
    assert any("prioritizes" in warning for warning in warnings)


def test_complete_input_keeps_original_order_without_coverage_warning(settings, multi_context):
    payload, _, warnings = prepare_review_input(multi_context, settings)
    assert [file["filename"] for file in payload["changed_files"]] == [
        file.filename for file in multi_context.changed_files
    ]
    assert not warnings


def test_changed_line_windows_keep_real_line_numbers_after_long_context():
    patch = "@@ -1,101 +1,102 @@\n" + " unchanged\n" * 100 + "-old\n+new\n+next\n"
    clipped = bounded_patch(patch, 160)
    assert len(clipped) <= 160 and "+new" in clipped and "+next" in clipped
    assert added_line_numbers(clipped) == {101, 102}


def test_budget_can_skip_an_unfit_hunk_and_keep_a_later_change():
    patch = "@@ -0,0 +1 @@\n+" + "x" * 500 + "\n@@ -10 +11 @@\n-old\n+fixed\n"
    clipped = bounded_patch(patch, 128)
    assert "+fixed" in clipped and "x" * 500 not in clipped
    assert added_line_numbers(clipped) == {11}


def test_compact_windows_preserve_insertion_and_deletion_headers():
    addition = "@@ -10,0 +11,50 @@\n" + "+value\n" * 50
    deletion = "@@ -20,50 +19,0 @@\n" + "-value\n" * 50
    assert bounded_patch(addition, 128).startswith("@@ -10,0 +11,")
    assert bounded_patch(deletion, 128).startswith("@@ -20,")
    assert "+19,0 @@" in bounded_patch(deletion, 128)


def test_truncated_multi_hunk_diff_does_not_invent_citations():
    patch = "@@ -1 +1 @@\n-old\n+new\n" + " context\n" * 60 + "@@ -80 +90 @@\n-before\n+after\n"
    clipped = bounded_patch(patch, 160)
    assert added_line_numbers(clipped) == {1, 90}
    assert len(clipped) <= 160


def test_oversized_source_line_leaves_room_for_another_file(settings, multi_context):
    context = multi_context.model_copy(
        update={
            "changed_files": [
                changed("src/auth.py", "@@ -1 +1 @@\n+" + "x" * 1000),
                changed("src/other.py"),
            ]
        }
    )
    _, patches, warnings = prepare_review_input(
        context, settings.model_copy(update={"max_patch_chars": 128})
    )
    assert list(patches) == ["src/other.py"] and warnings


def test_limited_clean_review_cannot_approve(settings, multi_context, multi_job):
    context = multi_context.model_copy(
        update={"changed_files": [changed("src/one.py"), changed("src/two.py")]}
    )

    async def run():
        workflow = ReviewWorkflow(
            reviewers=create_reviewers(
                MockLLMProvider(), settings.model_copy(update={"max_files_per_review": 1})
            ),
            mock_mode=True,
        )
        await workflow.start(multi_job, context)
        report = await workflow.report(multi_job.review_id)
        assert report.status == "PARTIAL"
        assert report.review.recommendation == "COMMENT" and not report.review.findings
        assert all(result.coverage_warnings for result in report.agent_results.values())

    asyncio.run(run())
