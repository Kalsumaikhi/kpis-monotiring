from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://kpi:kpi@localhost:5432/kpi"
    mattermost_webhook_url: str | None = None
    dashboard_base_url: str = "http://localhost:8000"
    dashboard_password: str = ""
    raw_dir: str = "/data/raw"
    raw_retention_runs: int = 5
    suspect_removal_ratio: float = 0.2
    consecutive_failure_alert: int = 3
    # Comma-separated hostnames/IPs that watch targets may reach even if they resolve to
    # loopback/private/link-local addresses (internal pages). Default empty: public only.
    watch_allowed_hosts: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
