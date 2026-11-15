"""Create Workspace takes the repository folders and the agents' access in the one dialog."""

import json

from tests.browser.conftest import seed


def test_create_workspace_saves_folders_and_gives_ticked_agents_access(instance, admin_page):
    page = admin_page
    seed(instance, (
        "from app.database import get_db\n"
        "from app.services import agent_service\n"
        "with get_db() as c: owner = c.execute('SELECT id FROM users LIMIT 1').fetchone()['id']\n"
        "agent_service.create_agent(agent_id='ws-codex', display_name='WS Codex', owner_user_id=owner, read_scopes=[], write_scopes=[])\n"
        "agent_service.create_agent(agent_id='ws-other', display_name='WS Other', owner_user_id=owner, read_scopes=[], write_scopes=[])\n"
    ))
    page.goto(instance + "/workspaces")
    page.wait_for_load_state("load")
    page.click("text=+ Create Workspace")
    page.fill("#cp-id", "mediastudio")
    page.fill("#cp-name", "Media Studio")
    page.fill("#cp-repo-roots .ep-repo-root", "D:\\windows\\MediaStudio")
    page.click("#create-workspace-modal >> text=Add folder")
    page.fill("#cp-repo-roots .repo-root-row:last-child .ep-repo-root", "/mnt/nas/media")
    page.check("#cp-agents input[value=ws-codex]")
    page.click("#create-workspace-form button[type=submit]")
    page.wait_for_load_state("load")
    page.wait_for_selector("text=mediastudio")

    got = page.evaluate("fetch('/api/workspaces/mediastudio').then(r => r.json())")
    assert got["data"]["workspace"]["repo_roots"] == ["D:\\windows\\MediaStudio", "/mnt/nas/media"]
    agents = {}
    for agent_id in ("ws-codex", "ws-other"):
        data = page.evaluate("id => fetch('/api/agents/' + id).then(r => r.json())", agent_id)["data"]["agent"]
        agents[agent_id] = data
    codex = agents["ws-codex"]
    assert "workspace:mediastudio" in json.dumps(codex.get("read_scopes", codex.get("read_scopes_json")))
    assert "workspace:mediastudio" in json.dumps(codex.get("write_scopes", codex.get("write_scopes_json")))
    other = agents["ws-other"]
    assert "workspace:mediastudio" not in json.dumps(other.get("read_scopes", other.get("read_scopes_json")))
