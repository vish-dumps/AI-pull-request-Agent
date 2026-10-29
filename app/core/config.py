from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl, PostgresDsn, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_name: str = "AI Pull Request Review Agent"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://review_agent:review_agent@localhost:5432/review_agent"
    )
    redis_url: RedisDsn = RedisDsn("redis://localhost:6379/0")
    health_check_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    github_token: SecretStr = SecretStr("")
    github_webhook_secret: SecretStr = SecretStr("")
    github_api_base_url: HttpUrl = HttpUrl("https://api.github.com")
    github_api_version: str = "2026-03-10"
    github_request_timeout_seconds: float = Field(default=15, gt=0, le=120)
    github_max_attempts: int = Field(default=3, ge=1, le=3)
    github_max_file_bytes: int = Field(default=1_000_000, ge=1, le=1_000_000)
    max_webhook_body_bytes: int = Field(default=2_000_000, ge=1, le=25_000_000)
    webhook_enqueue_timeout_seconds: float = Field(default=3, gt=0, le=5)
    review_job_ttl_seconds: int = Field(default=86400, ge=60)
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_base_url: HttpUrl | None = None
    llm_mock_mode: bool = True
    llm_output_mode: Literal["json_schema", "json_object"] = "json_schema"
    llm_request_timeout_seconds: float = Field(default=30, gt=0, le=120)
    llm_max_attempts: int = Field(default=3, ge=1, le=3)
    llm_max_output_tokens: int = Field(default=2048, ge=128, le=8192)
    llm_token_limit_field: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    max_files_per_review: int = Field(default=30, ge=1, le=300)
    max_patch_chars: int = Field(default=60000, ge=128, le=200000)

    @field_validator("llm_base_url", mode="before")
    @classmethod
    def empty_llm_url(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("database_url")
    @classmethod
    def require_asyncpg(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg driver")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
