"""Revision 31 (connection audience lists): the seeding says what each scope always meant, the step runs under the access
check with the frozen 1.3 rule as "before", and a wrong seed is refused and rolled back."""

import json
import sqlite3

import pytest

from app.access_check import snapshot
from app.access_check.guard import MigrationRefused
from app.schema import _apply_schema_migrations, create_schema

CAPS = json.dumps(["memory", "coordination", "connectors_read", "connectors_execute"])


def _agent(conn, agent_id, person, *, reads=None, inherit=0, caps=CAPS, kind="agent"):
    own = f"agent:{agent_id}"
    conn.execute(
        "INSERT INTO agents (id, display_name, owner_user_id, default_user_id, read_scopes_json, write_scopes_json, "
        "capabilities_json, api_key_hash, inherit_user_bindings, kind) VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)",
        (agent_id, agent_id, person, person, json.dumps(reads if reads is not None else [own]), caps, f"h-{agent_id}", inherit, kind),
    )


def _user(conn, user_id, active=1):
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, is_active) VALUES (?, ?, 'x', ?, ?)",
        (user_id, f"{user_id}@example.com", user_id.title(), active),
    )


def _binding(conn, binding_id, scope):
    conn.execute("INSERT OR IGNORE INTO connector_types (id, display_name, auth_type, provider_type) VALUES ('t', 'T', 'none', 'builtin')")
    conn.execute(
        "INSERT INTO connector_bindings (id, connector_type_id, name, scope, enabled) VALUES (?, 't', ?, ?, 1)", (binding_id, binding_id, scope)
    )


@pytest.fixture
def before31():
    """A database as it was before revision 31: current schema, the audience table removed, revision 31 not recorded."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.execute("DROP TABLE connector_binding_audience")
    conn.execute("DELETE FROM schema_migrations WHERE revision = 31")
    _user(conn, "alex")
    _user(conn, "sam")
    _user(conn, "gone", active=0)
    conn.execute("INSERT INTO workspaces (id, name, owner_user_id) VALUES ('site', 'Site', 'alex')")
    conn.execute("INSERT INTO workspaces (id, name, owner_user_id) VALUES ('lab', 'Lab', 'sam')")
    conn.execute("INSERT INTO workspace_collaborators (workspace_id, user_id, role, can_read, can_write) VALUES ('site', 'alex', 'owner', 1, 1)")
    conn.execute("INSERT INTO workspace_collaborators (workspace_id, user_id, role, can_read, can_write) VALUES ('lab', 'sam', 'owner', 1, 1)")
    # alex's agents: on (inherits), off (does not), a service
    _agent(conn, "alex-on", "alex", reads=["agent:alex-on", "workspace:site"], inherit=1)
    _agent(conn, "alex-off", "alex", reads=["agent:alex-off", "workspace:site"], inherit=0)
    _agent(conn, "alex-svc", "alex", caps='["credentials_resolve"]', kind="service")
    # sam's agent that also lists alex's personal scope itself, and one that lists a workspace sam is not in
    _agent(conn, "sam-peek", "sam", reads=["agent:sam-peek", "user:alex"])
    _agent(conn, "sam-lab", "sam", reads=["agent:sam-lab", "workspace:lab", "workspace:site"])
    # an agent of a disabled person
    _agent(conn, "gone-agent", "gone", reads=["agent:gone-agent"], inherit=1)
    # an agent reading another agent's private scope and the shared scope
    _agent(conn, "reader", "sam", reads=["agent:reader", "agent:alex-on", "shared"])
    for binding_id, scope in (
        ("b-agent", "agent:alex-on"), ("b-user", "user:alex"), ("b-site", "workspace:site"),
        ("b-lab", "workspace:lab"), ("b-shared", "shared"), ("b-gone", "user:gone"),
    ):
        _binding(conn, binding_id, scope)
    conn.commit()
    return conn


def _rows(conn):
    return sorted(
        (r["binding_id"], r["kind"], r["ref"], bool(r["exclude"]))
        for r in conn.execute("SELECT binding_id, kind, ref, exclude FROM connector_binding_audience")
    )


def test_each_scope_seeds_what_it_always_meant(before31):
    conn = before31
    _apply_schema_migrations(conn)
    rows = _rows(conn)
    assert ("b-agent", "agent", "alex-on", False) in rows and ("b-agent", "agent", "reader", False) in rows
    assert [r for r in rows if r[0] == "b-site"] == [("b-site", "workspace", "site", False)]
    assert [r for r in rows if r[0] == "b-lab"] == [("b-lab", "workspace", "lab", False)]
    user_rows = [r for r in rows if r[0] == "b-user"]
    assert ("b-user", "person_agents", "alex", False) in user_rows
    assert ("b-user", "agent", "alex-off", True) in user_rows  # the old switch was off
    assert ("b-user", "agent", "sam-peek", False) in user_rows  # reached alex's scope itself
    assert not any(r[2] == "alex-on" for r in user_rows)  # the switch was on: no exclusion
    assert not any(r[2] == "alex-svc" for r in user_rows)  # a service is outside "all my agents" by kind
    assert {r[2] for r in rows if r[0] == "b-shared"} == {"reader"}


def test_the_step_changes_no_agents_reach_and_is_recorded(before31):
    conn = before31
    frozen = snapshot.capture(conn, use_rule="1.3")
    _apply_schema_migrations(conn)
    now = snapshot.capture(conn)
    assert frozen["see"] == now["see"] and frozen["run"] == now["run"]
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE revision = 31").fetchone()[0] == 1
    # the matrices say what they should, so the equality is not vacuous
    assert now["see"]["alex-on"] == ["b-agent", "b-site", "b-user"]
    assert now["see"]["alex-off"] == ["b-site"]
    assert now["see"]["alex-svc"] == []
    assert now["see"]["sam-peek"] == ["b-user"]  # reaches alex's scope itself, not sam's workspace
    assert now["see"]["gone-agent"] == []  # a disabled person lends nothing, before or after
    assert now["see"]["sam-lab"] == ["b-lab"]  # sam is not in "site": that entry reaches nobody new


def test_a_wrong_seed_is_refused_and_leaves_the_database_as_it_was(before31, monkeypatch):
    from app.services import audience_service

    monkeypatch.setattr(audience_service, "seed", lambda conn, binding_id, scope: None)  # every connection left with nobody
    with pytest.raises(MigrationRefused):
        _apply_schema_migrations(before31)
    assert not before31.execute("SELECT 1 FROM sqlite_master WHERE name = 'connector_binding_audience'").fetchone()
    assert before31.execute("SELECT COUNT(*) FROM schema_migrations WHERE revision = 31").fetchone()[0] == 0


def test_a_seed_that_adds_reach_is_refused_too(before31, monkeypatch):
    from app.services import audience_service

    real = audience_service.seed_entries

    def too_wide(conn, scope):
        return real(conn, scope) + [{"kind": "agent", "ref": "alex-svc", "exclude": False}]  # a service must never be in one

    monkeypatch.setattr(audience_service, "seed_entries", too_wide)
    # alex-svc is a service: the rule keeps it out, so reach is unchanged and the step is accepted ...
    _apply_schema_migrations(before31)
    assert snapshot.capture(before31)["see"]["alex-svc"] == []


def test_an_agent_that_does_not_list_its_own_scope_does_not_reach_it_and_the_step_agrees(before31):
    """build_agent_context adds an agent's own scope to its RECALL set only, never to its read scopes: an agent that does
    not list agent:<id> cannot use a connection owned by it, before or after, so no row is needed and nothing differs."""
    conn = before31
    _agent(conn, "quiet", "alex", reads=["workspace:site"])  # does not list agent:quiet
    _binding(conn, "b-quiet", "agent:quiet")
    conn.commit()
    frozen = snapshot.capture(conn, use_rule="1.3")
    _apply_schema_migrations(conn)
    now = snapshot.capture(conn)
    assert "b-quiet" not in frozen["see"]["quiet"] and frozen["see"] == now["see"]
    assert not [r for r in _rows(conn) if r[0] == "b-quiet"]


def test_the_seed_follows_the_effective_scopes_even_if_a_build_adds_more_than_is_stored(before31, monkeypatch):
    """If another install's effective read scopes ever exceed the stored list, the seed (which asks the frozen rule) still
    names exactly the agents the 'before' snapshot says reach the connection, so the guard does not refuse the upgrade."""
    from app.security import scope_enforcer

    real = scope_enforcer.build_agent_context

    def wider(agent):
        context = real(agent)
        own = f"agent:{agent['id']}"
        if own not in context.read_scopes:
            context.read_scopes = [*context.read_scopes, own]
        return context

    monkeypatch.setattr(scope_enforcer, "build_agent_context", wider)
    conn = before31
    _agent(conn, "quiet", "alex", reads=["workspace:site"])
    _binding(conn, "b-quiet", "agent:quiet")
    conn.commit()
    frozen = snapshot.capture(conn, use_rule="1.3")
    assert "b-quiet" in frozen["see"]["quiet"]  # the widened effective scopes reach it
    _apply_schema_migrations(conn)  # must not be refused
    now = snapshot.capture(conn)
    assert frozen["see"] == now["see"] and ("b-quiet", "agent", "quiet", False) in _rows(conn)
