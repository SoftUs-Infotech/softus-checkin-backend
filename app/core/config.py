"""Environment-driven settings.

Fails fast at import time if a required value is missing, so a misconfigured
deployment never gets as far as serving traffic.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- app ---
    APP_NAME: str = "Softus Checkin"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    API_PREFIX: str = "/api/v1"

    # --- security ---
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    INVITE_EXPIRE_HOURS: int = 72
    PASSWORD_RESET_EXPIRE_MINUTES: int = 60

    # --- database ---
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB: str = "softus_checkin"

    # --- web ---
    CORS_ORIGINS: str = "http://localhost:5173"
    FRONTEND_URL: str = "http://localhost:5173"

    # --- organisation ---
    ORG_TIMEZONE: str = "Asia/Kolkata"

    # --- email ---
    EMAIL_PROVIDER: str = "console"
    EMAIL_FROM: str = "no-reply@softus.local"
    EMAIL_FROM_NAME: str = "Softus Checkin"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_TLS: bool = True

    LOG_DIR: str = Field(default="logs")

    @field_validator("SECRET_KEY")
    @classmethod
    def _secret_key_must_be_strong(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                "SECRET_KEY is required. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if len(value) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters long.")
        return value

    @field_validator("ORG_TIMEZONE")
    @classmethod
    def _timezone_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"ORG_TIMEZONE '{value}' is not a valid IANA timezone.") from exc
        return value

    @field_validator("EMAIL_PROVIDER")
    @classmethod
    def _known_email_provider(cls, value: str) -> str:
        allowed = {"console", "smtp"}
        normalised = value.strip().lower()
        if normalised not in allowed:
            raise ValueError(f"EMAIL_PROVIDER must be one of {sorted(allowed)}.")
        return normalised

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def org_tz(self) -> ZoneInfo:
        return ZoneInfo(self.ORG_TIMEZONE)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
