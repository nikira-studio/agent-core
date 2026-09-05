import re
from pathlib import Path

from app.routes.mcp import MANIFEST, _TOOL_CAPABILITIES, _manifest_for
from app.security.context import RequestContext
from app.security.effective_authority import EffectiveAuthority


def _authority(capabilities):
    return EffectiveAuthority(
        RequestContext(
            actor_type="agent",
            actor_id="test-agent",
            agent_id="test-agent",
            capabilities=frozenset(capabilities),
        )
    )


def test_each_manifest_tool_has_a_policy_or_is_explicitly_public():
    public_tools = {"effective_authority", "result_fetch"}
    manifest_names = {tool["name"] for tool in MANIFEST["tools"]}
    assert manifest_names == set(_TOOL_CAPABILITIES) | public_tools


def test_served_skill_tool_references_match_the_manifest():
    manifest_names = {tool["name"] for tool in MANIFEST["tools"]}
    skills_dir = Path(__file__).resolve().parents[2] / "app" / "agent_skills"
    for path in skills_dir.glob("*/SKILL.md"):
        content = path.read_text(encoding="utf-8")
        assert set(re.findall(r"`([a-z_]+)`", content)) <= manifest_names


def test_manifest_hides_tools_outside_agent_capabilities():
    visible = {tool["name"] for tool in _manifest_for(_authority({"memory"}))["tools"]}
    assert "memory_search" in visible
    assert "credential_list" not in visible
    assert "connectors_run" not in visible
    assert "effective_authority" in visible


def test_explicit_empty_capabilities_do_not_fall_back_to_full_authority():
    authority = _authority(set())
    assert not authority.has_capability("memory")
    assert not authority.can("memory", "read", scope="agent:test-agent")
