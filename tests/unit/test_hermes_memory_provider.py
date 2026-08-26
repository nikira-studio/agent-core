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
