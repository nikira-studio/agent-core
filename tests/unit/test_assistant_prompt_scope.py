"""Phase 5 regression: the generated assistant-onboarding prompt must never tell a
no-workspace agent to treat its private agent scope as a durable/shared store.

This is the contradiction that led Clawdia to silo personal facts in her private
agent scope. The remediation routes durable writes to a workspace (and tells a
no-workspace agent to request one); these tests lock that in so it can't silently
regress.
"""

from app.routes.dashboard import _build_assistants_md

BASE = "http://core.example.com"
USER = "user:brian"
AGENT = "agent:clawdia"
WORKSPACE = "workspace:agent-core"


def test_no_workspace_prompt_does_not_make_agent_scope_a_durable_store():
    md = _build_assistants_md(BASE, USER, None, AGENT)

    # Durable knowledge (fact/decision) must NOT be directed at the private scope.
    assert f"`decision` in `{AGENT}`" not in md
    assert f"`fact` in `{AGENT}`" not in md
    assert f"Write durable, shareable memory to `{AGENT}`" not in md

    # And it must not be described as a durable store anywhere.
    assert f"`{AGENT}` is your durable" not in md
    assert f"durable store in `{AGENT}`" not in md

    # The agent scope's only durable-write role is private scratch.
    assert f"`scratchpad` in your agent scope `{AGENT}`" in md

    # Positive steer: ask the owner for a workspace instead of repurposing the scope.
    assert "ask the owner to create" in md.lower()


def test_workspace_prompt_directs_durable_writes_to_the_workspace():
    md = _build_assistants_md(BASE, USER, WORKSPACE, AGENT)

    assert f"Write durable, shareable memory to `{WORKSPACE}`" in md
    assert f"`decision` in `{WORKSPACE}`" in md
    assert f"`fact` in `{WORKSPACE}`" in md
    # Even with a workspace, durable knowledge is never the private agent scope.
    assert f"`decision` in `{AGENT}`" not in md


def test_prompt_steers_other_project_scopes_to_on_demand():
    """Scope-routing guidance: a key with read access to other workspaces must be
    told to recall from its default scopes by default and reach into other
    projects only on demand (the cross-project recall-bleed fix)."""
    for md in (
        _build_assistants_md(BASE, USER, None, AGENT),
        _build_assistants_md(BASE, USER, WORKSPACE, AGENT),
    ):
        low = md.lower()
        # Names the failure mode explicitly.
        assert "unscoped" in low
        assert "on-demand" in low or "on demand" in low
        # Tells it not to fan recall across other readable workspaces by default.
        assert "do not fan recall across other workspaces" in low
        # And to only reach into another project when the request is about it.
        assert "explicitly about that project" in low or "explicitly about another project" in low
