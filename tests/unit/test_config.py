import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_configuration_reads_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://demo:demo@db:5432/demo")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/2")
    monkeypatch.setenv("HEALTH_CHECK_TIMEOUT_SECONDS", "1.5")
    settings = Settings(_env_file=None)
    assert settings.database_url.hosts()[0]["host"] == "db"
    assert settings.redis_url.host == "cache"
    assert settings.health_check_timeout_seconds == 1.5


def test_dotenv_accepts_future_phase_variables(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MOCK_MODE=true\nAPP_NAME=Demo\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.app_name == "Demo"


@pytest.mark.parametrize("url", ["not-a-url", "postgresql://user:pass@localhost/db"])
def test_database_url_requires_async_postgres_driver(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=url)


@pytest.mark.parametrize("timeout", [0, -1, 31])
def test_health_timeout_must_be_positive_and_bounded(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, health_check_timeout_seconds=timeout)


def test_invalid_configuration_hides_input_values():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="postgresql://user:super-secret@localhost/db")
    assert "super-secret" not in str(error.value)


def test_github_secrets_are_hidden_in_settings_representation():
    settings = Settings(
        _env_file=None,
        github_token="private-github-token",
        github_webhook_secret="private-webhook-secret",
    )
    assert "private-github-token" not in repr(settings)
    assert "private-webhook-secret" not in repr(settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"github_max_attempts": 0},
        {"github_max_attempts": 4},
        {"max_webhook_body_bytes": 0},
        {"webhook_enqueue_timeout_seconds": 6},
        {"review_job_ttl_seconds": 0},
    ],
)
def test_github_and_queue_limits_are_validated(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_empty_llm_url_is_accepted_in_mock_mode():
    settings = Settings(
        _env_file=None, llm_base_url="", llm_mock_mode=True, llm_api_key="private-llm-key"
    )
    assert settings.llm_base_url is None
    assert settings.llm_mock_mode is True
    assert "private-llm-key" not in repr(settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_max_attempts": 4},
        {"llm_max_output_tokens": 0},
        {"llm_output_mode": "unknown"},
        {"max_files_per_review": 0},
        {"max_patch_chars": 0},
    ],
)
def test_llm_limits_and_modes_are_validated(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)
