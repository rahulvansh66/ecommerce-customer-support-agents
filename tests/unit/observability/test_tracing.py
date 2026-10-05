"""Unit tests for the LangSmith tracing switch (environment only; no network)."""

import os

import pytest
from pydantic import SecretStr

from ecommerce_agents.config import Secrets, Settings, TracingConfig
from ecommerce_agents.observability.tracing import configure_tracing

TRACING_VARS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear tracing variables (and restore them after the test)."""
    for name in TRACING_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(enabled: bool = True, **secret_values: object) -> Settings:
    """Build settings with explicit tracing config and secrets (ignores the real .env)."""
    return Settings(
        tracing=TracingConfig(enabled=enabled, project="yaml-project"),
        secrets=Secrets(_env_file=None, **secret_values),
    )


def test_enabled_exports_env_and_uses_yaml_project() -> None:
    """With a key present, tracing is turned on and the YAML project is the default."""
    on = configure_tracing(make_settings(langsmith_api_key=SecretStr("ls-key")))
    assert on
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "ls-key"
    assert os.environ["LANGSMITH_PROJECT"] == "yaml-project"


def test_env_project_and_endpoint_override() -> None:
    """LANGSMITH_PROJECT / LANGSMITH_ENDPOINT from .env win over the YAML default."""
    configure_tracing(
        make_settings(
            langsmith_api_key=SecretStr("k"),
            langsmith_project="env-project",
            langsmith_endpoint="https://eu.api.smith.langchain.com",
        )
    )
    assert os.environ["LANGSMITH_PROJECT"] == "env-project"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://eu.api.smith.langchain.com"


def test_missing_key_disables() -> None:
    """No API key means tracing stays off."""
    assert not configure_tracing(make_settings())
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_config_switch_disables_even_with_key() -> None:
    """``tracing.enabled: false`` wins over a present key."""
    assert not configure_tracing(make_settings(enabled=False, langsmith_api_key=SecretStr("k")))
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_env_flag_false_disables() -> None:
    """``LANGSMITH_TRACING=false`` in .env turns tracing off."""
    settings = make_settings(langsmith_api_key=SecretStr("k"), langsmith_tracing=False)
    assert not configure_tracing(settings)
