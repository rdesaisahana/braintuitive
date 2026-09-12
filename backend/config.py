"""Application configuration.

All runtime configuration is read from environment variables (or a local
``.env`` file) exactly once, at import time, into a single ``settings``
singleton. Nothing in the codebase should read ``os.environ`` directly --
import ``settings`` instead so that every knob is discoverable in one place.

Example:
    from config import settings

    api_key = settings.NEBIUS_API_KEY
    timeout = settings.AGENT_TIMEOUT
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository layout: <project_root>/backend/config.py
BACKEND_DIR: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = BACKEND_DIR.parent
# Where the database and uploaded PDFs live. A host with a persistent disk
# points this at the disk (BRAINTUITIVE_DATA_DIR=/var/data on Render). The one
# environment read outside Settings: these paths are needed before it exists,
# so it must be a real environment variable, not a line in .env.
DATA_DIR: Path = Path(os.environ.get("BRAINTUITIVE_DATA_DIR") or PROJECT_ROOT / "data")
UPLOAD_DIR: Path = DATA_DIR / "uploads"


class Settings(BaseSettings):
    """Typed application settings, sourced from the environment."""

    model_config = SettingsConfigDict(
        # Both locations are accepted; backend/.env wins if both exist.
        env_file=(PROJECT_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # -- Application ---------------------------------------------------------
    APP_NAME: str = "Braintuitive"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # -- Server --------------------------------------------------------------
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    # Comma-separated in the environment: "http://localhost:3000,http://localhost:5173"
    CORS_ORIGINS: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )

    # -- Security ------------------------------------------------------------
    # MUST be overridden in production. Generate with: openssl rand -hex 32
    SECRET_KEY: str = "dev-only-insecure-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # -- SQLite (relational: users, quizzes, progress) -----------------------
    SQLITE_PATH: Path = DATA_DIR / "braintuitive.db"
    SQL_ECHO: bool = False

    # -- MongoDB (documents: curriculum chunks, configurations) --------------
    MONGODB_URI: str = ""
    MONGODB_DB_NAME: str = "braintuitive"
    MONGODB_TIMEOUT_MS: int = 5000

    # -- Nebius LLM (OpenAI-compatible wire protocol) ------------------------
    NEBIUS_API_KEY: str = ""
    NEBIUS_BASE_URL: str = "https://api.studio.nebius.com/v1"
    # Benchmarked on this account: Qwen3-235B ran ~171 tok/s vs ~7 tok/s for
    # Llama-3.3-70B on the same question-generation prompt. Re-measure before
    # switching -- provider serving capacity, not parameter count, decides.
    NEBIUS_MODEL: str = "Qwen/Qwen3-235B-A22B-Instruct-2507"
    NEBIUS_EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-8B"
    # Verified against the live API: Qwen3-Embedding-8B returns 4096 dims.
    EMBEDDING_DIMENSIONS: int = 4096
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 2048
    LLM_REQUEST_TIMEOUT: int = 180

    # -- Pinecone (vector store, free tier) ----------------------------------
    PINECONE_API_KEY: str = ""
    PINECONE_INDEX_NAME: str = "braintuitive-curriculum"
    PINECONE_CLOUD: str = "aws"
    PINECONE_REGION: str = "us-east-1"
    PINECONE_METRIC: str = "cosine"

    # -- RAG pipeline --------------------------------------------------------
    CHUNK_SIZE_TOKENS: int = 500
    CHUNK_OVERLAP_TOKENS: int = 100
    RETRIEVAL_TOP_K: int = 5
    RETRIEVAL_MIN_SCORE: float = 0.35
    MAX_UPLOAD_MB: int = 50

    # -- Agents --------------------------------------------------------------
    AGENT_TIMEOUT: int = 120  # seconds, wall clock for one execute()
    AGENT_MAX_ITERATIONS: int = 8  # ReAct loops before the executor stops
    AGENT_VERBOSE: bool = True

    # -- Question bank (pre-generation) --------------------------------------
    # Questions held per (sub-unit, difficulty). At 10 per quiz, 30 gives a
    # student three completely fresh attempts before anything repeats.
    BANK_DEPTH: int = 30
    # How far through their current unit a student must be before the NEXT
    # unit starts being pre-generated. Filling the next unit on day one wastes
    # generation on units a student may never reach.
    BANK_LOOKAHEAD_TRIGGER: float = 0.7
    # Generation budget for a single scheduled refill run (~12s per question).
    BANK_REFILL_BUDGET: int = 60
    # Filled immediately when a parent uploads a curriculum, so their
    # child's first session is instant rather than waiting ~36s on the
    # model. Three slots; the scheduler covers the rest within its
    # interval, by which time nobody has got that far.
    BANK_PRIME_BUDGET: int = 90

    # -- Quiz rules ----------------------------------------------------------
    QUESTIONS_PER_QUIZ: int = 10
    BEGINNER_THRESHOLD: float = 70.0
    INTERMEDIATE_THRESHOLD: float = 80.0
    PROFICIENT_THRESHOLD: float = 90.0

    # -- You.com search ------------------------------------------------------
    YOU_API_KEY: str = ""
    YOU_BASE_URL: str = "https://api.ydc-index.io"

    # -- Scheduler (APScheduler, in-process) ---------------------------------
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_TIMEZONE: str = "America/New_York"
    DAILY_REMINDER_HOUR: int = 17  # 5pm local
    WEEKLY_REPORT_DAY: str = "sun"

    # -- Email notifications -------------------------------------------------
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str = "no-reply@braintuitive.app"

    # -- Logging -------------------------------------------------------------
    LOG_LEVEL: str = "INFO"

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Allow CORS_ORIGINS to be given as a comma-separated string."""
        if isinstance(value, str):
            if not value.strip():
                return []
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("LOG_LEVEL")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid LOG_LEVEL: {value!r}")
        return level

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def SQLALCHEMY_DATABASE_URL(self) -> str:  # noqa: N802 - SQLAlchemy convention
        """SQLAlchemy URL for the SQLite file, with the parent dir ensured."""
        self.SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # as_posix() keeps the URL valid on Windows (C:/... not C:\...)
        return f"sqlite:///{self.SQLITE_PATH.as_posix()}"

    @property
    def mongodb_configured(self) -> bool:
        return bool(self.MONGODB_URI)

    @property
    def pinecone_configured(self) -> bool:
        return bool(self.PINECONE_API_KEY)

    @property
    def nebius_configured(self) -> bool:
        return bool(self.NEBIUS_API_KEY)

    def threshold_for(self, difficulty: str) -> float:
        """Return the passing score (%) required for a difficulty level."""
        return {
            "beginner": self.BEGINNER_THRESHOLD,
            "intermediate": self.INTERMEDIATE_THRESHOLD,
            "proficient": self.PROFICIENT_THRESHOLD,
        }[difficulty]

    def warn_on_missing_secrets(self) -> list[str]:
        """Return human-readable warnings for unset third-party integrations.

        Missing keys are deliberately non-fatal in development so the API still
        boots; the affected feature degrades instead of crashing the process.
        """
        warnings: list[str] = []
        if not self.nebius_configured:
            warnings.append("NEBIUS_API_KEY is not set - agents and embeddings are disabled.")
        if not self.pinecone_configured:
            warnings.append("PINECONE_API_KEY is not set - RAG retrieval is disabled.")
        if not self.mongodb_configured:
            warnings.append("MONGODB_URI is not set - document storage is disabled.")
        if self.is_production and self.SECRET_KEY == "dev-only-insecure-secret-change-me":
            warnings.append("SECRET_KEY is still the insecure default in production!")
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton (FastAPI dependency friendly)."""
    return Settings()


settings: Settings = get_settings()

# Make sure the on-disk directories exist before anything tries to write.
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def configure_logging() -> None:
    """Apply the project-wide logging format. Called once from ``main``."""
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # These libraries are extremely chatty at INFO.
    for noisy in ("httpx", "httpcore", "pymongo", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
