"""A row on Needs you says what it is about, what answering does, and where the real action is."""

from app.database import get_db
from app.services import activity_service, agent_service, workspace_service

WS = "workspace:ny-a"


def _mcp(client, key, tool, **params):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"tool": tool, "params": params})
    return r


def _ok(client, key, tool, **params):
    r = _mcp(client, key, tool, **params)
    assert r.status_code in (200, 201), (tool, r.text)
    return r.json()["data"]


def _setup():
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("ny-a", "Billing API", owner)
    _, key = agent_service.create_agent(agent_id="coder", display_name="Coding assistant", owner_user_id=owner, read_scopes=[WS, "agent:coder"], write_scopes=[WS, "agent:coder"])
    return key


def _ask(client, key, ex, task, text, **handoff):
    out = _ok(client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description=task, status="blocked",
              task_result="Waiting.", task_ref=f"tracker:{abs(hash(task)) % 10000}", handoff={"needs_human": text, **handoff})
    return out["activity"]["id"]


def test_each_row_leads_with_its_task_workspace_agent_and_age_and_says_what_answering_does(test_client, admin_token):
    key = _setup()
    same = "A reviewed candidate is ready for your decision. Try the preview, then accept or rework."
    # Tasks that belong to different tickets each keep their own open question, even from one agent.
    ex = {ref: _ok(test_client, key, "session_context", memory_scope=WS, host_session_ref=ref)["execution_id"] for ref in "abc"}
    linked = _ask(test_client, key, ex["c"], "Approve the rate limit change", "Decide this in the review tool.", needs_human_url="https://review.example.com/changes/42")
    second = _ask(test_client, key, ex["b"], "Update the 41 routine dependencies", same)
    first = _ask(test_client, key, ex["a"], "Add the subscriber count to the page", same)
    page = test_client.get("/").text
    waiting = page.split('id="overview-waiting"', 1)[1].split("</table>", 1)[0]
    # two identical questions are told apart by their tasks
    assert "Add the subscriber count to the page" in waiting and "Update the 41 routine dependencies" in waiting
    assert waiting.count("Billing API") >= 3 and "Coding assistant" in waiting and ("asked just now" in waiting or " ago" in waiting)
    assert ">Question<" not in waiting
    # what answering does, and the button for no answer
    assert "Your answer goes to Coding assistant the next time it works in Billing API, and this closes." in waiting
    assert "Mark as dealt with" in waiting and f"data-handoff-answer='{first}'" in waiting
    # what the task link opens
    assert f"href='/activity?task={first}'" in waiting and "See the task and its notes" in waiting
    # a link: the main action is there, the answer is secondary and does not claim to do it
    assert "Open in review.example.com" in waiting and "https://review.example.com/changes/42" in waiting
    assert "does not carry it out" in waiting
    detail = test_client.get(f"/activity?task={second}").text
    assert "Update the 41 routine dependencies" in detail.split('id="activity-focus"', 1)[1].split("</section>", 1)[0] or "Update the 41 routine dependencies" in detail


def test_the_link_is_validated_stored_and_listed(test_client, admin_token):
    key = _setup()
    exv = _ok(test_client, key, "session_context", memory_scope=WS, host_session_ref="v")["execution_id"]
    bad = _mcp(test_client, key, "activity_update", memory_scope=WS, execution_id=exv, task_description="Bad link", status="blocked", task_result="x",
               handoff={"needs_human": "Decide.", "needs_human_url": "javascript:alert(1)"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_HANDOFF"
    good = _ask(test_client, key, _ok(test_client, key, "session_context", memory_scope=WS, host_session_ref="w")["execution_id"], "Good link", "Decide it over there.", needs_human_url="https://tracker.example.com/issue/9")
    items = _ok(test_client, key, "attention_list")["items"]
    row = next(i for i in items if i["id"] == good)
    assert row["needs_human_url"] == "https://tracker.example.com/issue/9" and row["asked_at"]
    assert "needs_human_url" in activity_service.get_activity(good)["handoff_json"]


def test_attention_list_does_not_attach_an_empty_session_context(test_client, admin_token):
    key = _setup()
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("ny-b", "Second", owner)
    _, other = agent_service.create_agent(agent_id="two-ws", display_name="two-ws", owner_user_id=owner, read_scopes=[WS, "workspace:ny-b", "agent:two-ws"], write_scopes=["agent:two-ws"])
    data = _ok(test_client, other, "attention_list")  # two workspaces and no scope named: nothing to attach
    assert "session_context" not in data or data["session_context"].get("execution_id")
