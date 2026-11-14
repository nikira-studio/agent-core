"""Every inline script on every dashboard page parses: a stray brace once left the whole Webhooks page without its buttons."""

import re
import shutil
import subprocess

import pytest

PAGES = ["/", "/approvals", "/activity", "/audit", "/memory", "/agents", "/integrations", "/workspaces", "/connectors",
         "/credentials", "/connectors/adapters", "/connectors/help", "/connectors/directory", "/settings", "/users", "/webhooks"]


@pytest.fixture
def authenticated_client(test_client, clean_db):
    from app.services.auth_service import create_session, create_user

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    return test_client


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_inline_script_parses(authenticated_client, tmp_path):
    problems = []
    for path in PAGES:
        html = authenticated_client.get(path).text
        for index, script in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)):
            if not script.strip():
                continue
            file = tmp_path / f"{path.strip('/').replace('/', '_') or 'home'}-{index}.js"
            file.write_text(script)
            result = subprocess.run(["node", "--check", str(file)], capture_output=True, text=True)
            if result.returncode:
                problems.append((path, index, result.stderr.strip().splitlines()[:3]))
    assert not problems, problems
