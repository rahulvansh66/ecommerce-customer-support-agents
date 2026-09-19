"""Centralized configuration for the ecommerce agents.

This is the single place where YAML config (``config/<env>.yaml``) and secrets
(``.env`` / process environment) are read. Everything else in the codebase asks
:func:`get_settings` for typed values, so switching environment or moving to a
secret manager later touches only this module.

Role in the system: consumed by the LLM factory, DB pool, persistence factory,
agent graph, API and CLI.
"""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
CONFIG_DIR: Path = PROJECT_ROOT / "config"
PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"
DEFAULT_ENV = "dev"


class ConfigError(RuntimeError):
    """Raised when required configuration or secrets are missing or invalid."""


class AppConfig(BaseModel):
    """General application settings (``app`` section of the YAML)."""

    env: str = DEFAULT_ENV
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8001


class LLMConfig(BaseModel):
    """Chat model settings (``llm`` section of the YAML)."""

    provider: Literal["groq"] = "groq"
    model: str = "openai/gpt-oss-120b"
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout_s: float = 30.0
    max_retries: int = 2


class DatabaseConfig(BaseModel):
    """Neon Postgres settings (``database`` section of the YAML)."""

    schema_name: str = "order_management"
    pool_min_size: int = 1
    pool_max_size: int = 5
    pool_timeout_s: float = 15.0
    statement_timeout_ms: int = 10000
    max_rows: int = Field(5, description="Max rows a tool returns to the model.")


class BackendConfig(BaseModel):
    """Selects the backend for a persistence component."""

    backend: Literal["memory"] = "memory"


class PersistenceConfig(BaseModel):
    """LangGraph persistence settings (``persistence`` section of the YAML)."""

    checkpointer: BackendConfig = BackendConfig()
    store: BackendConfig = BackendConfig()


class AgentConfig(BaseModel):
    """Order management agent settings (``agent`` section of the YAML)."""

    prompt_file: str = "order_management.md"
    recursion_limit: int = 12
    max_history_messages: int = 20


class TracingConfig(BaseModel):
    """LangSmith tracing settings (``tracing`` section of the YAML).

    Attributes:
        enabled: Master switch; tracing also needs ``LANGSMITH_API_KEY`` in the environment.
        project: Default LangSmith project, used when ``LANGSMITH_PROJECT`` is not set.
    """

    enabled: bool = True
    project: str = "ecommerce-customer-support-agents"


class ApiConfig(BaseModel):
    """FastAPI settings (``api`` section of the YAML)."""

    cors_origins: list[str] = ["*"]


class Secrets(BaseSettings):
    """Secrets loaded from the process environment or the repo-root ``.env``.

    Attributes:
        neon_postgres_connection_string: Neon Postgres DSN.
        neon_postgres_test_connection_string: DSN of the separate test database
            (``neondb_test``); used only by the integration tests.
        groq_api_key: Primary Groq API key.
        groq_fallback_api_key: Optional secondary Groq key used if the primary fails.
        langsmith_api_key: LangSmith API key (tracing is skipped without it).
        langsmith_tracing: Optional ``LANGSMITH_TRACING`` flag from the environment.
        langsmith_endpoint: Optional LangSmith API endpoint (e.g. the EU region URL).
        langsmith_project: Optional LangSmith project name; overrides ``tracing.project``.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    neon_postgres_connection_string: SecretStr | None = None
    neon_postgres_test_connection_string: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    groq_fallback_api_key: SecretStr | None = None
    langsmith_api_key: SecretStr | None = None
    langsmith_tracing: bool | None = None
    langsmith_endpoint: str | None = None
    langsmith_project: str | None = None


class Settings(BaseModel):
    """Fully resolved, typed settings: YAML values plus secrets."""

    app: AppConfig = AppConfig()
    llm: LLMConfig = LLMConfig()
    database: DatabaseConfig = DatabaseConfig()
    persistence: PersistenceConfig = PersistenceConfig()
    agent: AgentConfig = AgentConfig()
    tracing: TracingConfig = TracingConfig()
    api: ApiConfig = ApiConfig()
    secrets: Secrets = Secrets()

    def require_database_url(self) -> str:
        """Return the Neon connection string.

        Returns:
            The DSN as a plain string.

        Raises:
            ConfigError: If ``NEON_POSTGRES_CONNECTION_STRING`` is not set.
        """
        value = self.secrets.neon_postgres_connection_string
        if value is None or not value.get_secret_value():
            raise ConfigError("NEON_POSTGRES_CONNECTION_STRING is not set (environment or .env)")
        return value.get_secret_value()

    def require_groq_api_key(self) -> str:
        """Return the primary Groq API key.

        Returns:
            The key as a plain string.

        Raises:
            ConfigError: If ``GROQ_API_KEY`` is not set.
        """
        value = self.secrets.groq_api_key
        if value is None or not value.get_secret_value():
            raise ConfigError("GROQ_API_KEY is not set (environment or .env)")
        return value.get_secret_value()

    def load_prompt(self) -> str:
        """Read the order management system prompt from ``prompts/``.

        Returns:
            The prompt text.

        Raises:
            ConfigError: If the configured prompt file does not exist.
        """
        path = PROMPTS_DIR / self.agent.prompt_file
        if not path.is_file():
            raise ConfigError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8").strip()


def load_settings(env: str | None = None, config_dir: Path | None = None) -> Settings:
    """Build :class:`Settings` from ``config/<env>.yaml`` plus secrets.

    Args:
        env: Environment name (``dev``, ``prod``, ...). Defaults to the ``APP_ENV``
            environment variable, then ``dev``.
        config_dir: Directory holding the YAML files. Defaults to ``<repo>/config``.

    Returns:
        The resolved settings.

    Raises:
        ConfigError: If the YAML file for the environment does not exist.
    """
    env_name = env or os.environ.get("APP_ENV", DEFAULT_ENV)
    path = (config_dir or CONFIG_DIR) / f"{env_name}.yaml"
    if not path.is_file():
        raise ConfigError(f"Config file not found for env={env_name}: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = Settings(**raw, secrets=Secrets())
    logger.info("component=config status=loaded env=%s file=%s", env_name, path.name)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings.

    Returns:
        The settings loaded by :func:`load_settings` on first call.

    Raises:
        ConfigError: If the YAML file for the current environment is missing.
    """
    return load_settings()
