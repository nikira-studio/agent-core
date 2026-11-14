"""Review: a person's answer travels back to the asking agent; lapsed asks say so."""

from datetime import timedelta

from app.database import get_db
from app.services import activity_service, agent_service, workspace_service
from app.services.activity_service import utc_now
from tests.conftest import csrf_headers

WS = "workspace:ri-a"


def _mcp(client, key, tool, **params):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"tool": tool, "params": params})
    assert r.status_code in (200, 201), (tool, r.text)
    return r.json()["data"]


def _setup():
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("ri-a", "ri-a", owner)
    _, key = agent_service.create_agent(agent_id="asker", display_name="asker", owner_user_id=owner, read_scopes=[WS, "agent:asker"], write_scopes=[WS, "agent:asker"])
    return key


def _ask(client, key, question="Which delimiter should exports use?"):
    ex = _mcp(client, key, "session_context", memory_scope=WS, host_session_ref="s1")["execution_id"]
    out = _mcp(client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Settle the export format",
               status="blocked", task_result="Waiting for the owner.", handoff={"needs_human": question})
    return out["activity"]["id"]


def test_the_answer_is_typed_on_needs_you_and_reaches_the_agents_next_session(test_client, admin_token):
    key = _setup()
    activity_id = _ask(test_client, key)
    page = test_client.get("/").text
    assert f"data-handoff-answer='{activity_id}'" in page and "Send answer" in page  # answered in place, not a link to Activity
    r = test_client.post(f"/api/activity/{activity_id}/handoff/resolve", headers=csrf_headers(test_client), json={"answer": "Use semicolons, UTF-8."})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["resolution"]["reason"] == "Use semicolons, UTF-8."
    assert _mcp(test_client, key, "attention_list")["items"] == []
    ctx = _mcp(test_client, key, "session_context", memory_scope=WS, host_session_ref="s2")
    text = str(ctx)
    assert "Which delimiter should exports use?" in text and "Use semicolons, UTF-8." in text and "resolved" in text.lower()


def test_marking_resolved_without_an_answer_still_works(test_client, admin_token):
    key = _setup()
    activity_id = _ask(test_client, key)
    r = test_client.post(f"/api/activity/{activity_id}/handoff/resolve", headers=csrf_headers(test_client))
    assert r.status_code == 200 and r.json()["data"]["resolution"]["kind"] == "person"
    assert "Use" not in str(_mcp(test_client, key, "session_context", memory_scope=WS, host_session_ref="s3")).split("resolved")[0][-40:]


def test_a_lapsed_ask_says_it_lapsed_on_the_activity_row_and_in_the_agents_context(test_client, admin_token):
    key = _setup()
    activity_id = _ask(test_client, key, "Pick a colour for the banner.")
    long_ago = (utc_now() - timedelta(days=9)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE agent_activity SET updated_at = ? WHERE id = ?", (long_ago, activity_id))
        conn.commit()
    activity_service.expire_unanswered_asks()
    html = test_client.get("/activity").text
    assert "lapsed" in html.lower()
    ctx = str(_mcp(test_client, key, "session_context", memory_scope=WS, host_session_ref="s4"))
    assert "Pick a colour for the banner." in ctx and "lapsed" in ctx.lower()
