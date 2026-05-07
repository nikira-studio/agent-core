import pytest
from app.models.enums import normalize_id, validate_id


def test_normalize_id_lowercases():
    assert normalize_id("Admin") == "admin"
    assert normalize_id("TEST-AGENT") == "test-agent"
    assert normalize_id("MyAgent") == "myagent"


def test_normalize_id_underscores_preserved():
    assert normalize_id("my_agent") == "my_agent"
    assert normalize_id("test_agent_123") == "test_agent_123"


def test_normalize_id_hyphens_preserved():
    assert normalize_id("my-agent") == "my-agent"


def test_normalize_id_max_length_64():
    long_id = "a" * 65
    with pytest.raises(ValueError):
        normalize_id(long_id)


def test_normalize_id_exactly_max_length():
    max_id = "a" * 64
    assert normalize_id(max_id) == max_id


def test_validate_id_valid():
    assert validate_id("admin") == True
    assert validate_id("test-agent") == True
    assert validate_id("agent123") == True
    assert validate_id("my_agent") == True


def test_validate_id_rejects_spaces():
    assert validate_id("my agent") == False


def test_validate_id_rejects_colon():
    assert validate_id("user:admin") == False


def test_validate_id_too_long():
    assert validate_id("a" * 65) == False