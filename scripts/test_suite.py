"""Run deterministic checks, or explicitly opt into PostgreSQL/PGVector and Redis."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--services", action="store_true", help="Require both TEST_* service URLs")
    parser.add_argument(
        "--layer", choices=["unit", "integration", "e2e"], help="Run one test layer"
    )
    args = parser.parse_args()
    environment = os.environ.copy()
    service_variables = ("TEST_DATABASE_URL", "TEST_REDIS_URL")
    if args.services:
        if any(not environment.get(name, "").strip() for name in service_variables):
            parser.error(
                "--services requires TEST_DATABASE_URL and TEST_REDIS_URL in the environment"
            )
    else:
        for name in service_variables:
            environment.pop(name, None)
    environment["LANGSMITH_TRACING"] = "false"
    root = Path(__file__).resolve().parents[1]
    commands = [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "pytest", "-q", *(["-m", args.layer] if args.layer else [])],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=root, env=environment, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
