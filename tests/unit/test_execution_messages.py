from app.services import workspace_sync_service as sync


def test_a_refused_execution_says_what_to_do():
    assert "session_context" in sync.execution_message("EXECUTION_NOT_FOUND")
    mismatch = sync.execution_message("EXECUTION_SCOPE_MISMATCH")
    assert "Leave execution_id out" in mismatch and "scope" in mismatch


def test_an_unknown_code_keeps_the_caller_supplied_wording():
    assert sync.execution_message("SOMETHING_ELSE") == "Invalid execution"
    assert sync.execution_message("SOMETHING_ELSE", "Invalid execution or cursor") == "Invalid execution or cursor"
