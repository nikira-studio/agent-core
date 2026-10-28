"""Regression coverage for the shipped Hermes memory provider."""

import importlib.util
import sys
import types
from pathlib import Path


PROVIDER_PATH = Path("integrations/hermes/memory-provider/__init__.py")


def _load_provider(monkeypatch):
    """Load the plugin without requiring a Hermes checkout in this test env."""
    agent_module = types.ModuleType("agent")
    memory_provider_module = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    memory_provider_module.MemoryProvider = MemoryProvider
    monkeypatch.setitem(sys.modules, "agent", agent_module)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider_module)

    spec = importlib.util.spec_from_file_location("test_hermes_agent_core", PROVIDER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provider_caps_injected_memory_by_character_count(monkeypatch):
    module = _load_provider(monkeypatch)
    provider = module.AgentCoreMemoryProvider()
    provider._max_context_chars = 80

    context = provider._format([{"content": "x" * 500}])

    assert len(context) <= 80
    assert context.endswith("...")


def test_provider_uses_a_conservative_default_context_budget(monkeypatch):
    monkeypatch.delenv("AGENT_CORE_MAX_CONTEXT_CHARS", raising=False)
    module = _load_provider(monkeypatch)
    provider = module.AgentCoreMemoryProvider()

    assert provider._max_context_chars == 12_000


def _write_config(tmp_path, authorization, url="https://core.example/mcp"):
    (tmp_path / "config.yaml").write_text(
        "mcp_servers:\n"
        "  agent_core:\n"
        f"    url: {url}\n"
        "    headers:\n"
        f"      Authorization: {authorization}\n"
    )


def _clear_env(monkeypatch, tmp_path):
    for name in ("AGENT_CORE_URL", "AGENT_CORE_API_KEY", "AGENT_CORE_BEARER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def test_provider_expands_env_placeholders_in_the_mcp_block(monkeypatch, tmp_path):
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CORE_AUTHORIZATION", "Bearer ac_sk_from_env")
    monkeypatch.setenv("CORE_HOST", "core.example")
    _write_config(tmp_path, '"${env:HERMES_CORE_AUTHORIZATION}"', url='"https://${CORE_HOST}/mcp"')
    module = _load_provider(monkeypatch)

    provider = module.AgentCoreMemoryProvider()

    assert provider._token == "ac_sk_from_env"
    assert provider._url == "https://core.example"
    assert provider.is_available()


def test_provider_with_an_unset_placeholder_is_unavailable_and_says_so(monkeypatch, tmp_path, caplog):
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_CORE_AUTHORIZATION", raising=False)
    _write_config(tmp_path, '"${env:HERMES_CORE_AUTHORIZATION}"')
    module = _load_provider(monkeypatch)

    with caplog.at_level("WARNING"):
        provider = module.AgentCoreMemoryProvider()

    assert provider._token == ""
    assert not provider.is_available()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "HERMES_CORE_AUTHORIZATION" in warnings[0] and "${" not in warnings[0]


def test_provider_never_logs_the_expanded_token(monkeypatch, tmp_path, caplog):
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CORE_AUTHORIZATION", "Bearer ac_sk_secret_value")
    _write_config(tmp_path, '"${env:HERMES_CORE_AUTHORIZATION}"')
    module = _load_provider(monkeypatch)

    with caplog.at_level("DEBUG"):
        module.AgentCoreMemoryProvider().initialize("s")

    assert "ac_sk_secret_value" not in caplog.text
