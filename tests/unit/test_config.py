"""Unit tests for the centralized config loader."""

from pathlib import Path

import pytest

from ecommerce_agents.config import ConfigError, Secrets, Settings, load_settings


def test_load_settings_reads_yaml(tmp_path: Path) -> None:
    """Values from ``<env>.yaml`` override the defaults."""
    (tmp_path / "test.yaml").write_text("llm:\n  model: some-model\nagent:\n  recursion_limit: 3\n")
    loaded = load_settings(env="test", config_dir=tmp_path)
    assert loaded.llm.model == "some-model"
    assert loaded.agent.recursion_limit == 3
    assert loaded.database.max_rows == 5  # default kept


def test_load_settings_missing_file(tmp_path: Path) -> None:
    """A missing environment file raises ``ConfigError``."""
    with pytest.raises(ConfigError):
        load_settings(env="nope", config_dir=tmp_path)


def test_missing_secrets_raise(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requiring an unset secret raises ``ConfigError``."""
    monkeypatch.delenv("NEON_POSTGRES_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    empty = Settings(secrets=Secrets(_env_file=None))
    with pytest.raises(ConfigError):
        empty.require_database_url()
    with pytest.raises(ConfigError):
        empty.require_groq_api_key()


def test_secrets_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets come from environment variables."""
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("NEON_POSTGRES_CONNECTION_STRING", "postgresql://x")
    built = Settings(secrets=Secrets(_env_file=None))
    assert built.require_groq_api_key() == "k"
    assert built.require_database_url() == "postgresql://x"


def test_prompt_loads(settings: Settings) -> None:
    """The configured prompt file exists and is non-empty."""
    assert "Order Management" in settings.load_prompt()
