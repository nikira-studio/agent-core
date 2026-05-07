import pytest
from app.services.memory_service import _sanitize_fts_query


def test_sanitize_wraps_tokens_in_quotes():
    result = _sanitize_fts_query("hello world")
    assert '"hello"' in result
    assert '"world"' in result


def test_sanitize_joins_with_and():
    result = _sanitize_fts_query("hello world")
    assert " AND " in result


def test_sanitize_removes_quotes_from_input():
    result = _sanitize_fts_query('"hello world"')
    assert '"hello"' in result
    assert '"world"' in result


def test_sanitize_removes_parentheses():
    result = _sanitize_fts_query("(hello)")
    assert "(" not in result


def test_sanitize_removes_wildcards():
    result = _sanitize_fts_query("test*")
    assert "*" not in result


def test_sanitize_removes_hyphen():
    result = _sanitize_fts_query("user-admin-name")
    assert "-" not in result


def test_sanitize_empty():
    assert _sanitize_fts_query("") == ""


def test_sanitize_truncates_long_input():
    long_query = "a" * 600
    result = _sanitize_fts_query(long_query)
    assert len(result) < 600


def test_sanitize_strips_whitespace():
    result = _sanitize_fts_query("  hello world  ")
    assert result.startswith('"hello"')