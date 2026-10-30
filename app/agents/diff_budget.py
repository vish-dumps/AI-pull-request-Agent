"""Deterministic file selection and changed-line windows; never execute PR text."""

import re
from pathlib import PurePosixPath

HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
IMPORTANT = re.compile(
    r"(^|[/_.-])(auth|authentication|authorization|security|permission|permissions|"
    r"payment|payments|billing|database|db|sql|migration|migrations|config|settings)([/_.-]|$)"
)


def file_priority(filename: str) -> int:
    path = filename.lower()
    parts = PurePosixPath(path).parts
    if any(
        part in {"vendor", "node_modules", "dist", "build", "generated"} for part in parts
    ) or path.endswith((".lock", ".min.js", ".map")):
        return 4
    if IMPORTANT.search(path):
        return 0
    if path.endswith((".md", ".rst", ".txt")) or "docs" in parts:
        return 3
    if any(part in {"test", "tests", "__tests__"} for part in parts) or PurePosixPath(
        path
    ).name.startswith("test_"):
        return 2
    return 1


def _windows(patch: str):
    """Yield (old cursor, new cursor, body) with two context lines per change."""
    lines = patch.splitlines()
    index = 0
    while index < len(lines):
        match = HEADER.match(lines[index])
        index += 1
        if match is None:
            continue
        old, new = int(match[1]), int(match[3])
        old += int(match[2] == "0")
        new += int(match[4] == "0")
        body = []
        while index < len(lines) and not HEADER.match(lines[index]):
            line = lines[index]
            if line.startswith((" ", "+", "-")):
                body.append(line)
            index += 1
        changes = [i for i, line in enumerate(body) if line.startswith(("+", "-"))]
        ranges = []
        for position in changes:
            start, end = max(0, position - 2), min(len(body), position + 3)
            if ranges and start <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
        previous = 0
        for start, end in ranges:
            for line in body[previous:start]:
                old += int(not line.startswith("+"))
                new += int(not line.startswith("-"))
            yield old, new, body[start:end]
            for line in body[start:end]:
                old += int(not line.startswith("+"))
                new += int(not line.startswith("-"))
            previous = end


def _render(old: int, new: int, body: list[str]) -> str:
    old_count = sum(not line.startswith("+") for line in body)
    new_count = sum(not line.startswith("-") for line in body)
    old_start = old if old_count else max(0, old - 1)
    new_start = new if new_count else max(0, new - 1)
    return f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n" + "\n".join(body)


def bounded_patch(patch: str, limit: int) -> str:
    if len(patch) <= limit:
        return patch
    windows = list(_windows(patch))
    if not windows:
        return patch[:limit].rsplit("\n", 1)[0] if "\n" in patch[:limit] else ""
    selected = []
    remaining = limit
    # Prefer additions, but keep deletions available to the evidence validator.
    ranked = sorted(
        enumerate(windows),
        key=lambda pair: (not any(line.startswith("+") for line in pair[1][2]), pair[0]),
    )
    for position, (old, new, body) in ranked:
        kept = []
        used = 0
        # A conservative header allowance avoids splitting long source lines.
        for line in body:
            if used + len(line) + 1 > remaining - 80:
                break
            kept.append(line)
            used += len(line) + 1
        if not any(line.startswith(("+", "-")) for line in kept):
            continue
        fragment = _render(old, new, kept)
        if len(fragment) <= remaining:
            selected.append((position, fragment))
            remaining -= len(fragment) + 1
    return "\n".join(fragment for _, fragment in sorted(selected))
