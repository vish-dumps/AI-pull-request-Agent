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
    redis_operation_timeout_seconds: float = Field(default=3, gt=0, le=30)
    worker_poll_seconds: float = Field(default=1, gt=0, le=30)
    worker_lease_seconds: float = Field(default=30, ge=3, le=300)
    worker_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_retry_seconds: float = Field(default=10, ge=1, le=300)
    worker_job_timeout_seconds: float = Field(default=600, ge=1, le=1800)
    worker_shutdown_grace_seconds: float = Field(default=15, ge=0, le=60)
    worker_maintenance_batch: int = Field(default=50, ge=1, le=100)
    worker_dead_letter_limit: int = Field(default=1000, ge=1, le=10000)
    pr_cache_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    pr_cache_max_bytes: int = Field(default=500000, ge=1024, le=2000000)
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
    agent_timeout_seconds: float = Field(default=210, gt=0, le=600)
    retrieval_enabled: bool = False
    embedding_mock_mode: bool = True
    embedding_base_url: HttpUrl | None = None
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = ""
    embedding_dimensions: int = Field(default=128, ge=8, le=2000)
    embedding_send_dimensions: bool = False
    embedding_request_timeout_seconds: float = Field(default=30, gt=0, le=120)
    embedding_max_attempts: int = Field(default=3, ge=1, le=3)
    embedding_batch_size: int = Field(default=16, ge=1, le=32)
    index_max_files: int = Field(default=100, ge=1, le=1000)
    index_max_file_bytes: int = Field(default=100000, ge=1, le=1000000)
    index_max_total_bytes: int = Field(default=2000000, ge=1, le=10000000)
    index_max_chunks: int = Field(default=1000, ge=1, le=5000)
    index_chunk_chars: int = Field(default=1800, ge=128, le=6000)
    index_timeout_seconds: float = Field(default=180, gt=0, le=600)
    max_context_chunks: int = Field(default=3, ge=1, le=5)
    max_context_chars: int = Field(default=6000, ge=128, le=30000)
    retrieval_query_chars: int = Field(default=4000, ge=128, le=6000)
    retrieval_min_similarity: float = Field(default=0.15, ge=0, le=1)
    retrieval_timeout_seconds: float = Field(default=45, gt=0, le=120)

    @field_validator("llm_base_url", "embedding_base_url", mode="before")
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
