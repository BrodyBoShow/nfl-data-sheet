"""
Job: Load and validate environment configuration for all pipeline jobs.
Reads: environment variables (.env locally, GitHub Secrets in Actions)
Writes: nothing
Tier: n/a
Phase: P1
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_db_url: str = Field(alias="SUPABASE_DB_URL")
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_service_role_key: str | None = Field(default=None, alias="SUPABASE_SERVICE_ROLE_KEY")
    odds_api_key: str | None = Field(default=None, alias="ODDS_API_KEY")
    discord_webhook_url: str | None = Field(default=None, alias="DISCORD_WEBHOOK_URL")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
