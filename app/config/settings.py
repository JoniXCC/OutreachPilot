"""Application settings loaded from environment variables / `.env`.

All configuration lives here so that no module reads `os.environ` directly and
no credential is ever hard-coded.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ProviderName = Literal["gemini", "groq", "mock"]


class Settings(BaseSettings):
    """Typed configuration. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Modes -------------------------------------------------------------
    demo_mode: bool = True
    safe_mode: bool = True

    # --- AI ------------------------------------------------------------------
    ai_provider: ProviderName = "gemini"
    ai_fallback_provider: ProviderName | None = None
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash-lite"
    groq_api_key: SecretStr | None = None
    groq_model: str = "llama-3.1-8b-instant"
    ai_temperature: float = Field(default=0.3, ge=0.0, le=1.0)
    ai_max_output_tokens: int = Field(default=600, ge=64, le=4096)
    ai_timeout_seconds: float = Field(default=30.0, gt=0)
    ai_cache_enabled: bool = True

    # --- Gmail -----------------------------------------------------------------
    gmail_credentials_path: Path = Path("credentials.json")
    gmail_token_path: Path = Path("token.json")
    inbox_lookback_days: int = Field(default=14, ge=1, le=90)
    demo_mailbox_path: Path = Path("data/demo_mailbox.json")

    # --- Sender identity ---------------------------------------------------------
    sender_name: str = "Your Name"
    sender_title: str = ""
    include_opt_out_line: bool = True

    # --- Database ----------------------------------------------------------------
    database_url: str = "sqlite:///data/outreach.db"

    # --- Sending limits ------------------------------------------------------------
    max_emails_per_day: int = Field(default=20, ge=0, le=500)
    min_seconds_between_emails: int = Field(default=90, ge=0)
    max_followups: int = Field(default=2, ge=0, le=5)
    followup_1_days: int = Field(default=3, ge=1)
    followup_2_days: int = Field(default=5, ge=1)
    classification_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # --- Research ------------------------------------------------------------------
    research_timeout_seconds: float = Field(default=10.0, gt=0)
    research_max_pages: int = Field(default=2, ge=1, le=3)
    research_max_chars: int = Field(default=3500, ge=500, le=12000)
    research_max_bytes: int = Field(default=1_500_000, ge=50_000)
    research_min_seconds_between_requests: float = Field(default=2.0, ge=0)
    research_respect_robots_txt: bool = True
    research_user_agent: str = (
        "AI-Sales-Outreach-Research/1.0 (+portfolio project; low-volume homepage fetch)"
    )

    # --- Optional REST API -------------------------------------------------------------
    api_token: SecretStr | None = None

    # --- Logging ---------------------------------------------------------------------
    log_level: str = "INFO"
    log_file: Path = Path("data/logs/app.log")

    @field_validator("ai_fallback_provider", mode="before")
    @classmethod
    def _empty_fallback_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("gemini_api_key", "groq_api_key", "api_token", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ai_provider", "ai_fallback_provider", mode="before")
    @classmethod
    def _lower_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    # --- Helpers -----------------------------------------------------------------------
    def resolve_path(self, path: Path) -> Path:
        """Resolve a relative path against the project root."""
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def followup_intervals_days(self) -> list[int]:
        return [self.followup_1_days, self.followup_2_days]

    @property
    def sqlite_path(self) -> Path | None:
        """Filesystem path of the SQLite DB (None for in-memory / other engines)."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        raw = self.database_url[len(prefix):]
        if not raw or raw == ":memory:":
            return None
        return self.resolve_path(Path(raw))

    def resolved_database_url(self) -> str:
        path = self.sqlite_path
        if path is None:
            return self.database_url
        return f"sqlite:///{path.as_posix()}"

    def public_view(self) -> dict[str, object]:
        """Settings safe to display in the dashboard (secrets masked)."""
        data = self.model_dump()
        for key in ("gemini_api_key", "groq_api_key", "api_token"):
            data[key] = "configured" if getattr(self, key) else "not set"
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in data.items()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
