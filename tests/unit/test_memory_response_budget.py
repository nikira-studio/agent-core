"""Whole-record prefix contracts independent of transport or the database."""

import json

import pytest

from app.operations.memory import MemoryOperationError, budget_search_response


def _size(data):
    return len(json.dumps(data, indent=2, default=str))


def test_longest_prefix_includes_envelope_unicode_and_escaping():
    records = [
        {"id": str(i), "content": '日本語 "quote" \\ newline\n' * (i + 1)}
        for i in range(10)
    ]
    original = {
        "records": records,
        "total": 10,
        "retrieval_mode": "fts_only",
        "metadata": "x" * 80,
    }
    for count in range(1, 10):
        prefix = {
            **original,
            "records": records[:count],
            "total": count,
            "response_limited": True,
        }
        bound = _size(prefix)
        result = budget_search_response(original, bound)
        assert result == prefix
        assert _size(result) <= bound
        if count == 1:
            # The explanatory empty result can be larger than one tiny record.
            with pytest.raises(MemoryOperationError):
                budget_search_response(original, bound - 1)
        else:
            smaller = budget_search_response(original, bound - 1)
            assert len(smaller["records"]) < count
    complete = {**original, "response_limited": False}
    assert budget_search_response(original, _size(complete)) == complete
    assert original["records"] == records
    assert "response_limited" not in original


def test_large_first_record_does_not_admit_a_lower_ranked_small_record():
    original = {
        "records": [
            {"id": "first", "content": "x" * 10000},
            {"id": "second", "content": "tiny"},
        ],
        "total": 2,
    }
    result = budget_search_response(original, 600)
    assert result["records"] == []
    assert result["response_limited"] is True
    assert result["warnings"][0]["code"] == "RESPONSE_BUDGET_TOO_SMALL"
    assert _size(result) <= 600


def test_operation_errors_propagate_through_context_managers():
    from contextlib import contextmanager

    @contextmanager
    def boundary():
        yield

    with pytest.raises(MemoryOperationError, match="INVALID_RESPONSE_BUDGET"):
        with boundary():
            from app.operations.memory import validate_response_budget

            validate_response_budget(False)
