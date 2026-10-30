import asyncio

import httpx
import pytest

from app.core.config import Settings


def test_implicit_local_dotenv_is_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=developer-token\nLLM_MOCK_MODE=false\n", encoding="utf-8")
    assert Settings().github_token.get_secret_value() == ""
    assert Settings().llm_mock_mode is True
    assert Settings(_env_file=env).github_token.get_secret_value() == "developer-token"


def test_native_sync_http_requires_fixture_transport():
    with httpx.Client() as client:
        with pytest.raises(AssertionError, match="Native HTTP is disabled"):
            client.get("https://external.invalid")


def test_native_async_http_requires_fixture_transport():
    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(AssertionError, match="Native HTTP is disabled"):
                await client.post("https://external.invalid", json={})

    asyncio.run(run())
