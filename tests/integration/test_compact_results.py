"""Lists agents receive inline: compact activity pages and a short key-records index.

Large activity lists used to pass the tool-result offload threshold and come back
as a handle that agents rarely fetched; the session-start index listed up to 40
records that agents almost never opened.
"""

import json

from app.config import settings
from app.database import get_db
from app.services import activity_service, agent_service, memory_service, workspace_service

SCOPE = "workspace:compact-test"
LONG = "word " * 800  # about 4,000 characters


def _agent():
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("compact-test", "Compact Test", owner_id)
    _, key = agent_service.create_agent(
        agent_id="compact-agent", display_name="Compact Agent", owner_user_id=owner_id,
        read_scopes=[SCOPE], write_scopes=[SCOPE],
    )
    return {"Authorization": f"Bearer {key}"}


def _mcp(client, headers, tool, **params):
    return client.post("/mcp", headers=headers, json={"tool": tool, "params": params})


def _long_activities(count):
    ids = []
    for n in range(count):
        activity = activity_service.create_activity(
            agent_id="compact-agent", user_id="u", task_description=f"Long task {n} " + LONG,
            memory_scope=SCOPE,
            metadata_json=activity_service.with_task_ref(None, f"tracker:{n}"),
        )
        activity_service.update_activity(activity["id"], status="blocked", task_note=LONG, task_result=LONG)
        activity_service.set_handoff(activity["id"], {"needs_human": "Decide " + LONG})
        ids.append(activity["id"])
    return ids


def test_activity_list_is_compact_and_fits_inline_by_default(test_client, admin_token):
    headers = _agent()
    _long_activities(30)

    data = _mcp(test_client, headers, "activity_list").json()["data"]

    assert len(json.dumps(data, indent=2)) <= settings.TOOL_RESULT_SPILL_THRESHOLD
    assert data["response_limited"] is True and 0 < data["count"] == len(data["activities"]) < 30
    row = data["activities"][0]
    assert "metadata_json" not in row and "handoff_json" not in row
    assert row["task_result"].endswith("…") and len(row["task_result"]) <= 240
    assert row["needs_human"].startswith("Decide") and row["task_ref"].startswith("tracker:")

    page_two = _mcp(test_client, headers, "activity_list", offset=data["count"]).json()["data"]
    assert {a["id"] for a in page_two["activities"]}.isdisjoint({a["id"] for a in data["activities"]})


def test_view_full_returns_whole_rows(test_client, admin_token):
    headers = _agent()
    _long_activities(2)

    rows = _mcp(test_client, headers, "activity_list", view="full").json()["data"]["activities"]

    assert len(rows) == 2 and rows[0]["task_result"] == LONG
    assert json.loads(rows[0]["metadata_json"])["task_ref"].startswith("tracker:")
    assert json.loads(rows[0]["handoff_json"])["needs_human"].startswith("Decide")


def test_activity_search_is_compact_by_default(test_client, admin_token):
    headers = _agent()
    _long_activities(3)

    data = _mcp(test_client, headers, "activity_search", query="Long task").json()["data"]

    assert data["count"] == 3 and all("metadata_json" not in a for a in data["activities"])
    assert _mcp(test_client, headers, "activity_search", query="Long task", view="sideways").status_code == 400


def test_session_context_lists_at_most_ten_key_records(test_client, admin_token):
    headers = _agent()
    for n in range(25):
        memory_service.write_memory(f"Decision number {n} about the build.", "decision", SCOPE, topic=f"d{n}")
    # Work after the records were written: they count as seen, so they are listed
    # in the index rather than under "changed since your last session".
    activity_service.create_activity(agent_id="compact-agent", user_id="u",
                                     task_description="Earlier work", memory_scope=SCOPE)

    response = test_client.post("/api/session-context", headers=headers, json={"memory_scope": SCOPE})
    text = response.json()["data"]["context"]

    assert "## Key records (10 of 25 active)" in text
