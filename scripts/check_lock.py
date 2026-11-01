"""Check dependency metadata and the hash-locked Docker requirements export."""

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    subprocess.run(["uv", "lock", "--check", "--offline"], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix="review-lock-") as directory:
        exported = Path(directory) / "docker.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--extra",
                "build",
                "--no-dev",
                "--no-editable",
                "--no-emit-project",
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(exported),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if exported.read_text(encoding="utf-8") != (ROOT / "requirements/docker.txt").read_text(
            encoding="utf-8"
        ):
            raise SystemExit("Docker requirements differ from uv.lock; regenerate the export.")
    print("Lock metadata and Docker requirements match.")


if __name__ == "__main__":
    main()
