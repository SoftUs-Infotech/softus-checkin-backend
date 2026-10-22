"""Environment-driven settings.

Fails fast at import time if a required value is missing, so a misconfigured
deployment never gets as far as serving traffic.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
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
    #: ``production`` hides the interactive API docs, refuses the demo seeder,
    #: and checks at start-up that the web settings point somewhere real.
    ENVIRONMENT: str = "development"
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

    # --- two-factor authentication (administrators) ---
    # The challenge that stands between a correct password and a token pair is
    # deliberately short-lived: it is a handover, not a session.
    TWO_FACTOR_CHALLENGE_EXPIRE_MINUTES: int = 5
    TWO_FACTOR_RECOVERY_CODES: int = 10
    # Steps of 30 seconds accepted either side of now, for clock drift between
    # the phone and the server. One step each way is the RFC 6238 suggestion;
    # widening it widens the window an intercepted code stays usable in.
    TWO_FACTOR_TIME_WINDOW: int = 1
    # What the authenticator app labels the account with. Falls back to APP_NAME.
    TWO_FACTOR_ISSUER: str = ""

    # --- database ---
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB: str = "softus_checkin"

    # --- web ---
    CORS_ORIGINS: str = "http://localhost:5173"
    #: Optional, and refused in production. A pattern for origins that cannot be
    #: listed in advance — every Vercel preview deployment has its own URL. Pin it
    #: to your team's scope, e.g. ``https://softus-checkin-[a-z0-9]+-yourteam\.vercel\.app``:
    #: a bare ``softus-checkin-.*`` matches projects anybody can create.
    CORS_ORIGIN_REGEX: str = ""
    FRONTEND_URL: str = "http://localhost:5173"

    # Which upstream hops may be believed about who the caller is.
    #
    # `X-Forwarded-For` is a header, and a header is whatever the sender typed.
    # Anybody can put the office's address in one, so it is only consulted when
    # the machine that actually opened the connection is listed here — and even
    # then the chain is read from the right, because a proxy *appends* the peer
    # it saw and only the hops it added are trustworthy. Everything to the left
    # of them is the client's own text.
    #
    # This is what stands between the office-network rule and anybody who can
    # type `curl -H`, so widening it is a real decision. Loopback is the default
    # because the API only ever sees loopback when something on the same host
    # forwarded the request — a reverse proxy or a tunnel client. Behind a proxy
    # on another host, name that host here; behind one that does *not* strip
    # inbound `X-Forwarded-For`, the rule cannot be enforced at all.
    # Comma-separated addresses or CIDR ranges. Empty means trust nothing.
    TRUSTED_PROXIES: str = "127.0.0.1,::1"

    # --- organisation ---
    ORG_TIMEZONE: str = "Asia/Kolkata"

    # --- geocoding ---
    # Optional. Without it a punch stores its coordinates and no street address;
    # nothing else changes, because a third party being unreachable must never
    # be able to fail a punch.
    GOOGLE_MAPS_API_KEY: str = ""

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

    @field_validator("ENVIRONMENT")
    @classmethod
    def _known_environment(cls, value: str) -> str:
        normalised = value.strip().lower()
        if normalised not in {"development", "production"}:
            raise ValueError("ENVIRONMENT must be 'development' or 'production'.")
        return normalised

    @model_validator(mode="after")
    def _production_must_point_somewhere_real(self) -> "Settings":
        """Refuse to start a production API that would hand out broken links.

        Each of these has shipped once already in some form: an invite email
        whose link pointed at a laptop, or a browser origin that never matched.
        """
        if not self.is_production:
            return self
        problems = []
        if self.DEBUG:
            problems.append("DEBUG must be false")
        if not self.FRONTEND_URL.startswith("https://") or "localhost" in self.FRONTEND_URL:
            problems.append("FRONTEND_URL must be the public https:// address of the web app")
        origins = self.cors_origins
        if not origins:
            problems.append("CORS_ORIGINS must list the web app's origin")
        if self.CORS_ORIGIN_REGEX:
            # A pattern is easy to get wider than intended, and previews of
            # unreviewed code have no business with the real records anyway.
            problems.append(
                "CORS_ORIGIN_REGEX is not allowed in production — point preview "
                "deployments at a separate staging API"
            )
        if "*" in origins:
            problems.append("CORS_ORIGINS cannot be '*' — tokens are sent with every request")
        if problems:
            raise ValueError("Production settings are incomplete: " + "; ".join(problems) + ".")
        return self

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def trusted_proxies(self) -> list[str]:
        return [entry.strip() for entry in self.TRUSTED_PROXIES.split(",") if entry.strip()]

    @property
    def org_tz(self) -> ZoneInfo:
        return ZoneInfo(self.ORG_TIMEZONE)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
