"""Central application configuration.

All settings come from environment variables (optionally via a local ``.env``
file, which is git-ignored). Secrets use ``SecretStr`` so they are masked in
``repr()``/logs; call ``get_secret_value()`` only where the key is actually used.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # OpenAI. No default: a missing key is reported by /ready, never invented.
    openai_api_key: SecretStr | None = Field(default=None)
    openai_model: str = Field(default="gpt-4o-mini", min_length=1)

    # Application
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # API server (used by `python -m app.main`)
    api_host: str = Field(default="127.0.0.1", min_length=1)
    api_port: int = Field(default=8000, ge=1, le=65535)

    # SQL Server (Phase 2 simulation database). Nothing here is required to start
    # the API: missing/invalid database settings only affect database features.
    db_server: str = Field(default="localhost", pattern=r"^[^;{}=\s]+$")
    db_name: str = Field(default="dataops_sim", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    db_auth_mode: Literal["windows", "sql"] = "windows"
    db_user: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.@\\-]{1,128}$")
    db_password: SecretStr | None = None
    db_driver: str = Field(
        default="ODBC Driver 18 for SQL Server", pattern=r"^[A-Za-z0-9 ._()\-]+$"
    )
    db_encrypt: bool = True
    # Local development only: lets ODBC Driver 18 accept SQL Server's self-signed cert.
    db_trust_server_certificate: bool = False
    db_login_timeout_seconds: int = Field(default=5, ge=1, le=60)

    # Simulation
    sim_seed: int = 20260115
    sim_log_dir: str = Field(default="logs", min_length=1)
    freshness_threshold_minutes: int = Field(default=1500, ge=1)

    @field_validator("openai_api_key", "db_user", "db_password", mode="before")
    @classmethod
    def _blank_key_is_missing(cls, value: object) -> object:
        """Treat an empty/whitespace value (e.g. ``OPENAI_API_KEY=``) as not set."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level", "app_env", mode="before")
    @classmethod
    def _normalise_case(cls, value: object, info) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value.upper() if info.field_name == "log_level" else value.lower()
        return value

    @property
    def openai_configured(self) -> bool:
        return self.openai_api_key is not None


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process. Tests clear the cache via ``cache_clear()``."""
    return Settings()
