from app.security.scope_utils import validate_scope_string, normalize_scope_string


def test_validate_user_scope():
    assert validate_scope_string("user:admin") is True
    assert validate_scope_string("user:user123") is True


def test_validate_agent_scope():
    assert validate_scope_string("agent:agent1") is True
    assert validate_scope_string("agent:test-agent") is True


def test_validate_workspace_scope():
    assert validate_scope_string("workspace:myproject") is True
    assert validate_scope_string("workspace:proj123") is True


def test_validate_rejects_uppercase():
    assert validate_scope_string("user:Admin") is False
    assert validate_scope_string("agent:TestAgent") is False


def test_validate_rejects_spaces():
    assert validate_scope_string("user:admin user") is False
    assert validate_scope_string("agent:my agent") is False


def test_validate_rejects_colon_in_id():
    assert validate_scope_string("user:admin:extra") is False


def test_validate_rejects_empty():
    assert validate_scope_string("") is False
    assert validate_scope_string("user:") is False


def test_normalize_scope_string_lowercases():
    assert normalize_scope_string("user:Admin") == "user:admin"
    assert normalize_scope_string("user:ADMIN") == "user:admin"


def test_normalize_scope_string_preserves_valid():
    assert normalize_scope_string("user:admin") == "user:admin"
    assert normalize_scope_string("agent:test-agent") == "agent:test-agent"


def test_validate_shared_system():
    assert validate_scope_string("shared") is True
    assert validate_scope_string("system") is True
