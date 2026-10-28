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


def test_handoff_paragraph_is_generic_and_can_be_turned_off(monkeypatch):
    module = _load_provider(monkeypatch)
    monkeypatch.delenv("AGENT_CORE_HANDOFF_PROMPT", raising=False)
    block = module.AgentCoreMemoryProvider().system_prompt_block()
    assert "attention_list" in block and "task_ref" in block

    monkeypatch.setenv("AGENT_CORE_HANDOFF_PROMPT", "0")
    assert "attention_list" not in module.AgentCoreMemoryProvider().system_prompt_block()


def _only_ruamel(monkeypatch):
    """A Hermes venv with ruamel.yaml and no PyYAML (the config is written as
    JSON, which is valid YAML, so the stand-in parser can use json)."""
    import json

    ruamel = types.ModuleType("ruamel")
    ruamel_yaml = types.ModuleType("ruamel.yaml")

    class YAML:
        def __init__(self, typ=None):
            assert typ == "safe"

        def load(self, text):
            return json.loads(text)

    ruamel_yaml.YAML = YAML
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setitem(sys.modules, "ruamel", ruamel)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", ruamel_yaml)


def test_provider_reads_the_config_with_ruamel_when_pyyaml_is_missing(monkeypatch, tmp_path):
    import json

    _clear_env(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text(json.dumps({"mcp_servers": {"agent_core": {
        "url": "https://core.example/mcp", "headers": {"Authorization": "Bearer ac_sk_ruamel"}}}}))
    _only_ruamel(monkeypatch)
    module = _load_provider(monkeypatch)

    provider = module.AgentCoreMemoryProvider()

    assert provider._token == "ac_sk_ruamel" and provider._url == "https://core.example"


def test_provider_without_any_yaml_parser_warns_once(monkeypatch, tmp_path, caplog):
    _clear_env(monkeypatch, tmp_path)
    _write_config(tmp_path, '"Bearer ac_sk_unread"')
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setitem(sys.modules, "ruamel", None)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", None)
    module = _load_provider(monkeypatch)

    with caplog.at_level("WARNING"):
        provider = module.AgentCoreMemoryProvider()

    assert not provider.is_available()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "PyYAML" in warnings[0] and "ac_sk_unread" not in caplog.text


# Inside Hermes the provider reads its config through Hermes itself and resolves
# placeholders from the active profile's secret scope (a ContextVar), not from
# os.environ. Synthetic stand-ins for those Hermes modules below.

def _secret_scope(make_get_secret):
    """A stand-in agent.secret_scope; make_get_secret(scope) returns get_secret."""
    scope = types.ModuleType("agent.secret_scope")
    scope.UnscopedSecretError = type("UnscopedSecretError", (RuntimeError,), {})
    scope.get_secret = make_get_secret(scope)
    return scope


def _hermes_config(config):
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    module = types.ModuleType("hermes_cli.config")
    module.load_config_readonly = lambda: config
    return {"hermes_cli": package, "hermes_cli.config": module}


def test_placeholder_uses_the_profile_scoped_secret(monkeypatch):
    module = _load_provider(monkeypatch)
    calls = []

    def get_secret(name):
        calls.append(name)
        return "synthetic-test-value"

    monkeypatch.setitem(sys.modules, "agent.secret_scope", _secret_scope(lambda scope: get_secret))
    monkeypatch.delenv("HERMES_CORE_AUTHORIZATION", raising=False)

    assert module._expand_env("${env:HERMES_CORE_AUTHORIZATION}") == ("synthetic-test-value", [])
    assert calls == ["HERMES_CORE_AUTHORIZATION"]


def test_unscoped_profile_secret_fails_closed_without_env_fallback(monkeypatch):
    module = _load_provider(monkeypatch)

    def unscoped(scope):
        def get_secret(_name):
            raise scope.UnscopedSecretError()
        return get_secret

    monkeypatch.setitem(sys.modules, "agent.secret_scope", _secret_scope(unscoped))
    monkeypatch.setenv("HERMES_CORE_AUTHORIZATION", "must-not-fallback")

    value, missing = module._expand_env("${env:HERMES_CORE_AUTHORIZATION}")
    assert value == "${env:HERMES_CORE_AUTHORIZATION}"
    assert missing == ["HERMES_CORE_AUTHORIZATION"]


def test_mcp_config_uses_the_hermes_readonly_loader(monkeypatch):
    module = _load_provider(monkeypatch)
    config = {"mcp_servers": {"agent_core": {
        "url": "https://core.invalid/mcp",
        "headers": {"Authorization": "Bearer synthetic-expanded-value"},
    }}}
    for name, mod in _hermes_config(config).items():
        monkeypatch.setitem(sys.modules, name, mod)

    assert module._agent_core_mcp_block() == config["mcp_servers"]["agent_core"]


def test_provider_initializes_from_the_profile_scope_without_process_env(monkeypatch):
    module = _load_provider(monkeypatch)
    for name in ("AGENT_CORE_URL", "AGENT_CORE_API_KEY", "AGENT_CORE_BEARER", "HERMES_CORE_AUTHORIZATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", _secret_scope(
        lambda scope: lambda name: "synthetic-test-value" if name == "HERMES_CORE_AUTHORIZATION" else None))
    for name, mod in _hermes_config({"mcp_servers": {"agent_core": {
        "url": "https://core.invalid/mcp",
        "headers": {"Authorization": "${env:HERMES_CORE_AUTHORIZATION}"},
    }}}).items():
        monkeypatch.setitem(sys.modules, name, mod)

    provider = module.AgentCoreMemoryProvider()

    assert provider.is_available()
    assert provider._url == "https://core.invalid"
    assert provider._token == "synthetic-test-value"


def test_yaml_loader_uses_the_hermes_safe_loader(monkeypatch, tmp_path):
    module = _load_provider(monkeypatch)
    utils = types.ModuleType("utils")
    utils.fast_safe_load = lambda stream: {"sentinel": stream.read()}
    monkeypatch.setitem(sys.modules, "utils", utils)
    path = tmp_path / "config.yaml"
    path.write_text("synthetic")

    assert module._load_yaml(str(path)) == {"sentinel": "synthetic"}
