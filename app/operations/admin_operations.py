"""The admin operations agents may propose. Importing this module registers them.

Each entry names an existing route handler; see admin_registry for how a
proposal runs it as the approving person. Add an operation here only when an
agent has a real need to prepare it for a person's approval.
"""

from typing import Optional

from app.database import get_db
from app.operations.admin_registry import register
from app.routes import agents, connectors, credential_consumers, credentials


def _row(table: str, key: str):
    """Fingerprint a row by its full contents, so any change since proposing shows."""
    def fingerprint(params: dict) -> Optional[str]:
        import hashlib
        import json

        with get_db() as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (params[key],)).fetchone()
        if row is None:
            return None
        return hashlib.sha256(json.dumps(dict(row), sort_keys=True, default=str).encode()).hexdigest()
    return fingerprint


register("agent.create", "Create an agent", agents.create_agent)
register("agent.update", "Change an agent's scopes, capabilities or description",
         agents.update_agent, fingerprint=_row("agents", "agent_id"))
register("credential_grant.create", "Let an agent's runtime resolve a credential",
         credential_consumers.create_grant)
register("credential_grant.revoke", "Revoke a credential grant",
         credential_consumers.revoke_grant, fingerprint=_row("credential_consumer_grants", "grant_id"))
register("credential.create", "Store a new credential (the person enters the value)",
         credentials.create_entry, person_fields=("value",))
register("binding.create", "Bind a connector to a credential in a scope", connectors.create_binding)
register("binding.update", "Change a connector binding",
         connectors.update_binding, fingerprint=_row("connector_bindings", "binding_id"))
register("connector_type.actions", "Enable or disable a connector type's actions",
         connectors.update_connector_type_actions, fingerprint=_row("connector_types", "connector_type_id"))
register("adapter.install", "Install a connector adapter", connectors.install_adapter)
