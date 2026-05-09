"""
CloudDrop — Centralized Configuration
All settings are loaded from environment variables with sensible defaults.
"""

import os


class Settings:
    # ── AWS ──────────────────────────────────────────────
    AWS_REGION: str = os.getenv("AWS_REGION", "ap-south-1")
    S3_BUCKET: str = os.getenv("S3_BUCKET", "clouddrop-files")
    KMS_KEY_ID: str = os.getenv("KMS_KEY_ID", "alias/clouddrop-key")

    # ── Redis (ElastiCache) ──────────────────────────────
    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_DB: int = int(os.getenv("REDIS_DB", "0"))
    REDIS_PASSWORD: str | None = os.getenv("REDIS_PASSWORD", None)
    REDIS_USE_TLS: bool = os.getenv("REDIS_USE_TLS", "false").lower() == "true"

    # ── Application ─────────────────────────────────────
    MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
    MAX_DOWNLOADS: int = int(os.getenv("MAX_DOWNLOADS", "5"))
    LINK_TTL_SECONDS: int = int(os.getenv("LINK_TTL_SECONDS", "86400"))  # 24 hours
    BASE_URL: str = os.getenv("BASE_URL", "http://localhost:8000")

    # ── PostgreSQL (RDS) ──────────────────────────────────
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")  # Required — no fallback
    DATABASE_READ_URL: str = os.getenv("DATABASE_READ_URL", "")  # Falls back to DATABASE_URL

    # ── Write-Back Sync ───────────────────────────────────
    DB_SYNC_INTERVAL_SECONDS: int = int(os.getenv("DB_SYNC_INTERVAL_SECONDS", "10"))

    # ── Cognito (app-level auth) ──────────────────────────
    COGNITO_REGION: str = os.getenv("COGNITO_REGION", AWS_REGION)
    COGNITO_USER_POOL_ID: str = os.getenv("COGNITO_USER_POOL_ID", "")
    COGNITO_CLIENT_ID: str = os.getenv("COGNITO_CLIENT_ID", "")
    COGNITO_CLIENT_SECRET: str = os.getenv("COGNITO_CLIENT_SECRET", "")
    COGNITO_DOMAIN: str = os.getenv("COGNITO_DOMAIN", "")  # e.g. clouddrop-auth.auth.ap-south-1.amazoncognito.com
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "clouddrop-dev-secret-change-me")


settings = Settings()
