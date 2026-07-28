import pytest


@pytest.fixture(autouse=True)
def _db(clean_db):
    pass


# Records taken from the live corpus, filed under the class they should have had.
CHOICES = [
    "Assistant-style agents such as the upstream framework should be told to update their own MCP "
    "config rather than being handed a hardcoded path.",
    "OpenCode sessions using deny-by-default tool permissions must explicitly allow "
    "Agent Core MCP tool patterns.",
    "When a workspace has a host-name collision, prefer the explicit IP/port in that "
    "workspace's AGENTS.md over the shared name.",
]

OBSERVATIONS = [
    "the build server is the home server at 192.0.2.10, and /srv/docker-data is the host-side "
    "mount root for container data.",
    "Schema upgrades temporarily disable SQLite foreign key enforcement while "
    "rebuilding connector tables. This is required because the rebuild recreates "
    "the table under a temporary name.",
    "Agent Core now uses a shared public-URL validation helper for connector fetches.",
]

# Real decisions that a lead-anchored test wrongly flagged: the rule is stated
# mid-sentence, or the choice is in the reasoning rather than the opening.
DECISIONS_STATING_THEIR_RULE_LATE = [
    "Agent-private memory scope (agent:<id>) is intentionally non-shared and should "
    "not be used as the handoff channel.",
    "The /audit page is a real admin-only page and should remain reachable from the "
    "admin sidebar.",
]

# Real facts that mention a rule further down without being one.
FACTS_THAT_MENTION_A_RULE = [
    "Connector interop gotcha (verified 2026-06-07 via the execution log). Some MCP "
    "clients serialize array tool-arguments as an object, so callers should send a "
    "JSON array explicitly.",
]


@pytest.mark.parametrize("content", CHOICES)
def test_choice_filed_as_fact_is_flagged(content):
    from app.services.memory_service import detect_class_mismatch

    assert detect_class_mismatch(content, "fact") is not None
    assert detect_class_mismatch(content, "decision") is None


@pytest.mark.parametrize("content", OBSERVATIONS)
def test_observation_filed_as_decision_is_flagged(content):
    from app.services.memory_service import detect_class_mismatch

    assert detect_class_mismatch(content, "decision") is not None
    assert detect_class_mismatch(content, "fact") is None


@pytest.mark.parametrize("content", DECISIONS_STATING_THEIR_RULE_LATE)
def test_decisions_that_state_their_rule_late_are_left_alone(content):
    from app.services.memory_service import detect_class_mismatch

    assert detect_class_mismatch(content, "decision") is None


@pytest.mark.parametrize("content", FACTS_THAT_MENTION_A_RULE)
def test_facts_that_merely_mention_a_rule_are_left_alone(content):
    from app.services.memory_service import detect_class_mismatch

    assert detect_class_mismatch(content, "fact") is None


def test_preferences_and_scratchpads_are_not_second_guessed():
    from app.services.memory_service import detect_class_mismatch

    assert detect_class_mismatch(CHOICES[0], "preference") is None
    assert detect_class_mismatch(OBSERVATIONS[0], "scratchpad") is None


def test_mismatch_surfaces_as_a_write_warning(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": CHOICES[1],
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        },
    )
    assert r.status_code == 201, r.json()
    data = r.json()["data"]
    assert "CLASS_MISMATCH" in [w["code"] for w in data.get("warnings", [])]
    # Advisory: the record is still written, under the class the caller chose.
    assert data["record"]["memory_class"] == "fact"


def test_correctly_classified_write_is_quiet(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": OBSERVATIONS[0],
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        },
    )
    assert r.status_code == 201, r.json()
    assert "warnings" not in r.json()["data"]


def test_mcp_schema_defines_the_classes(test_client, agent_token):
    """Agents were choosing a class from a bare enum with no definitions."""
    r = test_client.get("/mcp", headers={"Authorization": f"Bearer {agent_token}"})
    tool = next(t for t in r.json()["tools"] if t["name"] == "memory_write")
    described = tool["inputSchema"]["properties"]["memory_class"]["description"]
    assert "settled by checking" in described.lower()
    assert "settled by deciding" in described


def test_generated_agent_docs_explain_the_split(test_client, admin_token):
    from app.routes.integrations_page import (
        _build_claude_md,
        _build_agents_md,
        _build_instructions,
    )

    args = ("http://localhost:3500", "user:alex", "workspace:proj", "agent:codex")
    for text in (
        _build_claude_md(*args, "Codex", "Proj"),
        _build_agents_md(*args, "Proj"),
        _build_instructions(
            "http://localhost:3500", "codex", "user:alex", "workspace:proj",
            "agent:codex", "Codex", "Alex", "Proj",
        ),
    ):
        assert "settled by checking" in text, "the fact/decision test must be stated"
        assert "activity_update" in text


def test_every_agent_template_teaches_the_current_workflow():
    """The generated instructions are how an agent learns to use the system.

    They are easy to leave behind when a capability lands, and an agent that was
    never told about a tool will not use it — so the templates are asserted
    against, not just hand-checked.
    """
    from app.routes.integrations_page import (
        _build_agents_md,
        _build_assistants_md,
        _build_claude_md,
        _build_instructions,
        _build_session_prompt,
    )

    required = (
        "settled by checking",     # what separates a fact from a decision
        "subject_anchor",          # what would verify a fact
        "days_since_confirmed",    # how to read a stale record
        "memory_confirm",          # how to mark one checked
        "memory_feedback",         # how ranking learns
        "memory_reanchor",         # how to save a record whose subject moved
        "memory_pin",              # how to request standing context
        "valid_from",              # that "when it was true" is its own timeline
        "as_of",                   # how to ask what was true then
        "activity_search",         # where "what did we do" is answered
    )
    rendered = {
        "CLAUDE.md": _build_claude_md(
            "http://localhost:3500", "user:alex", "workspace:proj", "agent:codex", "Codex", "Proj"
        ),
        "AGENTS.md": _build_agents_md(
            "http://localhost:3500", "user:alex", "workspace:proj", "agent:codex", "Proj"
        ),
        "assistants": _build_assistants_md(
            "http://localhost:3500", "user:alex", "workspace:proj", "agent:codex"
        ),
        "instructions": _build_instructions(
            "http://localhost:3500", "codex", "user:alex", "workspace:proj",
            "agent:codex", "Codex", "Alex", "Proj",
        ),
        "session prompt": _build_session_prompt(
            "claude-code", "http://localhost:3500", "user:alex", "workspace:proj",
            "agent:codex", "Codex", "Alex", "Proj",
        ),
    }
    for name, text in rendered.items():
        for needle in required:
            assert needle in text, f"{name} never mentions {needle}"
        # Retired fields must not come back as advice.
        assert "domain/topic" not in text, f"{name} still teaches the retired domain field"


def test_the_verification_prompt_is_a_coherent_numbered_list():
    """It is pasted into a chat verbatim, so a broken sequence is user-visible."""
    import re

    from app.routes.integrations_page import _build_verification_prompt

    text = _build_verification_prompt("user:alex", "workspace:proj")
    numbers = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", text, re.M)]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    assert "activity_search" in text
