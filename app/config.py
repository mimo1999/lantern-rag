from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRETS = {"development-only-secret-change-me", "replace-with-a-long-random-secret"}


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    app_env: str = "development"
    jwt_issuer: str = "permission-aware-rag"
    jwt_secret_key: str = "development-only-secret-change-me"
    access_token_ttl_minutes: int = 60
    login_max_attempts: int = 3
    password_reset_ttl_minutes: int = 30
    # Requests per tenant per minute across all authenticated endpoints; 0 turns it off.
    tenant_rate_limit_per_minute: int = 120
    # Base of the link in reset emails (the UI reads the token from the URL fragment).
    public_base_url: str = "http://127.0.0.1:8000"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = "no-reply@localhost"
    embedding_model: str = "all-minilm:22m"
    embedding_dimension: int = 384
    # all-minilm accepts at most 512 tokens and Ollama truncates silently, so keep chunks small.
    chunk_size_chars: int = 1200
    chunk_overlap_chars: int = 150
    ollama_url: str = "http://localhost:11434"
    top_k: int = 20
    # Measured with all-minilm:22m on the synthetic benchmark; see README before changing.
    retrieval_relative_cutoff: float = 0.4
    # Tables and image captions from PDFs use their own embedding model (own vector table).
    # bge-m3 accepts long inputs (tables serialized to markdown) and outputs 1024 dims.
    asset_embedding_model: str = "bge-m3"
    asset_embedding_dimension: int = 1024
    # How many table/image chunks a query may add to the prose results.
    asset_top_k: int = 3
    # Vision-capable model that writes a searchable caption for each image in a PDF.
    vision_model: str = "gemma4:e2b"
    llm_model: str = "gemma4:e2b"
    max_context_chars: int = 12000
    max_upload_bytes: int = 10 * 1024 * 1024

    postgres_user: str = "postgres"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "permission_aware_rag"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    log_level: str = "INFO"
    metrics_token: SecretStr | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if not 0 <= self.retrieval_relative_cutoff <= 1:
            raise ValueError("retrieval cutoff must be between 0 and 1")
        if self.login_max_attempts < 1:
            raise ValueError("LOGIN_MAX_ATTEMPTS must be at least 1")
        if not 0 <= self.chunk_overlap_chars < self.chunk_size_chars:
            raise ValueError("CHUNK_OVERLAP_CHARS must be smaller than CHUNK_SIZE_CHARS")
        insecure = self.jwt_secret_key in _INSECURE_SECRETS or len(self.jwt_secret_key) < 32
        if self.app_env not in {"development", "test"} and insecure:
            raise ValueError("JWT_SECRET_KEY must be a random value of 32+ characters")
        return self

    @property
    def postgres_connect_kwargs(self) -> dict[str, str | int]:
        return {
            "host": self.postgres_host,
            "port": self.postgres_port,
            "user": self.postgres_user,
            "password": self.postgres_password.get_secret_value(),
            "database": self.postgres_db,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
