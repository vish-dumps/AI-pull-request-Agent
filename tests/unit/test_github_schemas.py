from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.github import ChangedFile, PullRequestWebhook, validate_repository
from app.schemas.jobs import ReviewJob


def test_payload_normalizes_fork_target(sample_payload):
    webhook = PullRequestWebhook.model_validate(sample_payload)
    assert webhook.repository.full_name == "octocat/Hello-World"
    assert webhook.pull_request.head.repo.full_name == "contributor/Hello-World"
    assert webhook.pull_request.head.sha == "b" * 40


@pytest.mark.parametrize("case", ["number", "base_repository", "sha", "boolean_number"])
def test_invalid_payload_relationships_are_rejected(sample_payload, case):
    payload = deepcopy(sample_payload)
    if case == "number":
        payload["pull_request"]["number"] = 43
    elif case == "base_repository":
        payload["pull_request"]["base"]["repo"]["full_name"] = "attacker/Other"
    elif case == "sha":
        payload["pull_request"]["head"]["sha"] = "not-a-sha"
    else:
        payload["number"] = True
    with pytest.raises(ValidationError):
        PullRequestWebhook.model_validate(payload)


@pytest.mark.parametrize(
    "repository", ["../repo", "owner/..", "owner/.", "https://evil/repo", "o/r/x"]
)
def test_repository_identifiers_reject_urls_and_traversal(repository):
    with pytest.raises(ValueError):
        validate_repository(repository)


def test_changed_file_allows_missing_patch_and_rename():
    file = ChangedFile(
        filename="assets/logo.png",
        status="renamed",
        previous_filename="logo.png",
        additions=0,
        deletions=0,
    )
    assert file.patch is None
    assert file.previous_filename == "logo.png"


def test_job_contract_rejects_unsupported_action(sample_job):
    with pytest.raises(ValidationError):
        ReviewJob.model_validate({**sample_job.model_dump(), "action": "closed"})
