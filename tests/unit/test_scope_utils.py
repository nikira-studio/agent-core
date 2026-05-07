import pytest
from app.security.scope_utils import validate_scope_string, normalize_scope_string


def test_validate_user_scope():
    assert validate_scope_string("user:admin") == True
    assert validate_scope_string("user:user123") == True


def test_validate_agent_scope():
    assert validate_scope_string("agent:agent1") == True
    assert validate_scope_string("agent:test-agent") == True


def test_validate_workspace_scope():
    assert validate_scope_string("workspace:myproject") == True
    assert validate_scope_string("workspace:proj123") == True


def test_validate_rejects_uppercase():
    assert validate_scope_string("user:Admin") == False
    assert validate_scope_string("agent:TestAgent") == False


def test_validate_rejects_spaces():
    assert validate_scope_string("user:admin user") == False
    assert validate_scope_string("agent:my agent") == False


def test_validate_rejects_colon_in_id():
    assert validate_scope_string("user:admin:extra") == False


def test_validate_rejects_empty():
    assert validate_scope_string("") == False
    assert validate_scope_string("user:") == False


def test_normalize_scope_string_lowercases():
    assert normalize_scope_string("user:Admin") == "user:admin"
    assert normalize_scope_string("user:ADMIN") == "user:admin"


def test_normalize_scope_string_preserves_valid():
    assert normalize_scope_string("user:admin") == "user:admin"
    assert normalize_scope_string("agent:test-agent") == "agent:test-agent"


def test_validate_shared_system():
    assert validate_scope_string("shared") == True
    assert validate_scope_string("system") == True
