"""Fetch normalized context for a real PR. This command only reads from GitHub."""

import argparse
import asyncio

from app.core.config import get_settings
from app.core.exceptions import GitHubAPIError
from app.services.github_service import GitHubService, build_github_client


async def fetch(repository: str, pr_number: int) -> None:
    settings = get_settings()
    async with build_github_client(settings) as client:
        context = await GitHubService(client, settings).get_pull_request_context(
            repository, pr_number
        )
        print(context.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="GitHub owner/repository")
    parser.add_argument("pr_number", type=int)
    args = parser.parse_args()
    try:
        asyncio.run(fetch(args.repository, args.pr_number))
    except (GitHubAPIError, ValueError) as error:
        parser.exit(1, f"Could not fetch PR context: {error}\n")


if __name__ == "__main__":
    main()
