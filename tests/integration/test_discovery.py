def test_public_discovery_document_has_only_connection_metadata(test_client):
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
    }
