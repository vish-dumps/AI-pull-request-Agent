"""Send a signed local sample delivery. No GitHub or LLM request is made."""

import argparse
import hashlib
import hmac
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from app.core.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000/webhooks/github")
    parser.add_argument(
        "--payload",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "examples/pull_request.opened.json",
    )
    parser.add_argument("--delivery-id", type=UUID, default=None)
    args = parser.parse_args()
    secret = get_settings().github_webhook_secret.get_secret_value()
    if not secret:
        parser.error("Set GITHUB_WEBHOOK_SECRET in the environment or .env first")
    body = args.payload.read_bytes()
    delivery = args.delivery_id or uuid4()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = httpx.post(
        args.url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(delivery),
            "X-Hub-Signature-256": signature,
        },
        timeout=10,
    )
    print(f"Delivery: {delivery}")
    print(f"HTTP {response.status_code}: {response.text}")
    response.raise_for_status()


if __name__ == "__main__":
    main()
