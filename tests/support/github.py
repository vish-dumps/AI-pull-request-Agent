import hashlib
import hmac
from uuid import uuid4


def webhook_headers(body, secret="test-webhook-secret", event="pull_request", delivery=None):
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": str(delivery or uuid4()),
        "X-Hub-Signature-256": "sha256="
        + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
    }


def pr_payload(context):
    return {
        "number": context.pr_number,
        "title": context.title,
        "body": context.description,
        "state": "open",
        "merged": False,
        "user": {"login": context.author},
        "base": {
            "ref": context.base_branch,
            "sha": context.base_sha,
            "repo": {"full_name": context.repository},
        },
        "head": {
            "ref": context.head_branch,
            "sha": context.head_sha,
            "repo": {"full_name": context.head_repository or context.repository},
        },
        "additions": context.total_additions,
        "deletions": context.total_deletions,
        "changed_files": context.total_changed_files,
    }
