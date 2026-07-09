"""Application configuration via Pydantic settings.

All configuration is sourced from environment variables (and an optional .env file).
Secrets are never logged; see app.logging for redaction of structured log values.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EraseAfterUnit(StrEnum):
    days = "days"
    hours = "hours"
    minutes = "minutes"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_env: str = "production"
    log_level: str = "INFO"
    poll_interval_seconds: int = 60
    # Delivery attempts before a submission is parked as dead_letter.
    max_attempts: int = 5
    request_timeout_seconds: int = 30

    # --- Management API ---
    api_host: str = "0.0.0.0"  # noqa: S104 - container binds all interfaces by design
    api_port: int = 8080

    # --- SQL Server (the poller's OWN database/schema; never a consumer's) ---
    database_url: str = Field(
        default="",
        description="SQLAlchemy mssql+pyodbc URL for the polling service's database.",
    )
    state_schema: str = "polling"
    # Create the [polling] schema/tables at startup if missing (runs migrations/*.sql).
    bootstrap_state_table: bool = True

    # --- Auth (JWT bearer; API key -> JWT via /auth/token) ---
    jwt_alg: str = "HS256"
    jwt_secret: str = ""  # required for HS256
    jwt_public_key: str = ""  # PEM, for RS256 (or use jwt_jwks_url)
    jwt_jwks_url: str = ""  # RS256 via a remote JWKS (external IdP)
    jwt_issuer: str = "os2forms-polling-service"
    jwt_audience: str = "os2forms-polling-service"
    jwt_ttl_seconds: int = 3600
    # Optional: seed an admin API key at startup if no admin key exists yet.
    bootstrap_admin_key: str = ""

    # --- OS2forms (Drupal webform_rest) ---
    os2forms_base_url: str = ""  # set via OS2FORMS_BASE_URL (see .env.example)
    os2forms_api_key: str = ""
    os2forms_verify_ssl: bool = True
    os2forms_timeout: int = 30

    # --- Automation Server (default destination) ---
    automation_server_url: str = ""
    automation_server_api_key: str = ""
    automation_server_api_prefix: str = "/api"
    automation_server_verify_ssl: bool = True
    automation_server_timeout: int = 30

    # --- Retention ---
    erase_after_unit: EraseAfterUnit = EraseAfterUnit.days


@lru_cache
def get_settings() -> Settings:
    return Settings()
