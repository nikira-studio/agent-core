from pathlib import Path

from app.branding import ENV_PREFIX
from app.config import Settings


def test_default_values():
    import os
    os.environ.pop(f"{ENV_PREFIX}DATA_PATH", None)
    s = Settings()
    assert s.PORT == 3500
    assert s.DATA_PATH == "./data"
    assert s.ENCRYPTION_KEY == "auto"
    assert s.SESSION_DURATION_HOURS == 8
    assert s.INACTIVITY_TIMEOUT_MINUTES == 30


def test_env_override(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "4000")
    monkeypatch.setenv(f"{ENV_PREFIX}ENCRYPTION_KEY", "test-key-32-bytes-long-here!!")
    s = Settings()
    assert s.PORT == 4000
    assert s.ENCRYPTION_KEY == "test-key-32-bytes-long-here!!"


def test_data_dir_relative():
    s = Settings(DATA_PATH="./data")
    expected = (Path(__file__).parents[2] / "data").resolve()
    assert s.data_dir == expected
    assert s.data_dir.exists()


def test_shared_scope_agent_list_empty():
    s = Settings(SHARED_SCOPE_AGENTS="")
    assert s.shared_scope_agent_list == []


def test_shared_scope_agent_list_parsed():
    s = Settings(SHARED_SCOPE_AGENTS="agent1, agent2, agent3")
    assert s.shared_scope_agent_list == ["agent1", "agent2", "agent3"]


def test_checked_in_compose_uses_auto_encryption_key():
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()
    assert f"{ENV_PREFIX}ENCRYPTION_KEY=auto" in compose
    assert f"{ENV_PREFIX}ENCRYPTION_KEY=autookay" not in compose
