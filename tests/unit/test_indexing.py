import pytest

from app.services.indexing_service import chunk_file, selected_path


@pytest.mark.parametrize(
    "path", ["src/a.py", "test/test_login.ts", "README.md", "main.cpp", "pkg/x.go"]
)
def test_common_source_and_docs_paths_are_selected(path):
    assert selected_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "../secret.py",
        "/etc/secret.py",
        "C:\\secret.py",
        "node_modules/lib/x.js",
        ".git/hooks/x.py",
        "build/main.py",
        "dist/a.ts",
        "generated/a.go",
        "src/a.generated.ts",
        "bundle.min.js",
        "src/a_pb2.py",
        "image.png",
        ".env",
        "src/\x00.py",
    ],
)
def test_unsafe_generated_binary_and_dependency_paths_are_skipped(path):
    assert not selected_path(path)


@pytest.mark.parametrize(
    "source", ["a\nb\nc\n", "x" * 4000, "猫" * 4000, "  \n\n", "first\n" + "x" * 300 + "\nlast"]
)
def test_chunking_keeps_source_and_correct_line_anchors(source):
    chunks = chunk_file("src/a.py", source, 128)
    assert all(
        len(chunk.content) <= 128 and len(chunk.content.encode()) <= 8192 for chunk in chunks
    )
    assert "".join(chunk.content for chunk in chunks) == source if source.strip() else chunks == []
    for chunk in chunks:
        assert chunk.end_line >= chunk.start_line
    if source.startswith("first"):
        assert chunks[0].start_line == chunks[0].end_line == 1
        assert chunks[-1].end_line == 3
