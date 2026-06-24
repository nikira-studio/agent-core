import json


def _seed_memory(scope, count):
    from app.services import memory_service

    for i in range(count):
        rec, err = memory_service.write_memory(
            content=f"record {i} marker " + ("y" * 400),
            memory_class="fact",
            scope=scope,
            topic=f"topic-{i}",
            provenance_json=json.dumps({"big": "z" * 500}),
        )
        assert err is None


def _tools_call(client, token, name, arguments, rid=10):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _call_text(resp):
    return resp.json()["result"]["content"][0]["text"]


def test_result_fetch_in_manifest(test_client, agent_token):
    r = test_client.get("/mcp", headers={"Authorization": f"Bearer {agent_token}"})
    tool_names = {t["name"] for t in r.json()["tools"]}
    assert "result_fetch" in tool_names


def test_large_tool_result_is_offloaded(test_client, agent_token):
    scope = "agent:testagent"
    _seed_memory(scope, 40)

    r = _tools_call(test_client, agent_token, "memory_get", {"scope": scope, "view": "full"})
    assert r.status_code == 200, r.json()
    assert r.json()["result"]["isError"] is False
    payload = json.loads(_call_text(r))
    assert payload["offloaded"] is True
    assert payload["handle"]
    assert payload["total_chars"] > 8000
    assert "summary" in payload
    assert payload["retrieve_with"]["tool"] == "result_fetch"


def test_result_fetch_reconstructs_full_payload(test_client, agent_token):
    scope = "agent:testagent"
    _seed_memory(scope, 40)

    r = _tools_call(test_client, agent_token, "memory_get", {"scope": scope, "view": "full"})
    payload = json.loads(_call_text(r))
    handle = payload["handle"]
    total = payload["total_chars"]

    chunks = []
    offset = 0
    while True:
        fr = _tools_call(
            test_client,
            agent_token,
            "result_fetch",
            {"handle": handle, "offset": offset, "limit": 4000},
        )
        assert fr.json()["result"]["isError"] is False
        d = json.loads(_call_text(fr))
        chunks.append(d["content"])
        if not d["has_more"]:
            break
        offset = d["next_offset"]

    full = "".join(chunks)
    assert len(full) == total
    assert "marker" in full


def test_small_tool_result_not_offloaded(test_client, agent_token):
    # Empty scope -> tiny payload, should pass through inline unchanged.
    r = _tools_call(test_client, agent_token, "memory_get", {"scope": "agent:testagent"})
    payload = json.loads(_call_text(r))
    assert "offloaded" not in payload
    assert "records" in payload


def test_result_fetch_unknown_handle_errors(test_client, agent_token):
    fr = _tools_call(test_client, agent_token, "result_fetch", {"handle": "nope-not-real"})
    assert fr.json()["result"]["isError"] is True
