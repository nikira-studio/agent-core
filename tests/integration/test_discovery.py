def test_public_discovery_document_links_to_instance_guidance(test_client):
    response = test_client.get(
        "/.well-known/agent-core.json",
        headers={"Host": "core.example.test"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "name": "Agent Core",
        "version": "1.0.0",
        "mcp_url": "http://core.example.test/mcp",
        "transport": "streamable-http",
        "authentication": {"type": "bearer"},
        "documentation_url": (
            "https://github.com/nikira-studio/agent-core/blob/main/"
            "docs/integrations.md"
        ),
        "setup_url": "http://core.example.test/setup.md",
        "llms_url": "http://core.example.test/llms.txt",
        "skills_url": "http://core.example.test/skills",
    }


def test_public_discovery_ignores_untrusted_forwarded_origin(test_client):
    response = test_client.get(
        "/.well-known/agent-core.json",
        headers={
            "Host": "core.example.test",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "attacker.example",
        },
    )
    assert response.json()["mcp_url"] == "http://core.example.test/mcp"


def test_public_discovery_honors_trusted_forwarded_origin(test_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "testclient")
    response = test_client.get(
        "/.well-known/agent-core.json",
        headers={
            "Host": "internal.example.test",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "core.example.test",
        },
    )
    assert response.json()["mcp_url"] == "https://core.example.test/mcp"


def test_served_skills_are_markdown_files_from_the_instance(test_client):
    listing = test_client.get("/skills", headers={"Host": "core.example.test"})
    assert listing.status_code == 200
    assert "agent-core-memory: http://core.example.test/skills/agent-core-memory" in listing.text

    skill = test_client.get("/skills/agent-core-memory")
    assert skill.status_code == 200
    assert skill.text.startswith("# Agent Core memory")
