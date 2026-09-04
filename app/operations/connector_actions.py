from dataclasses import dataclass
from typing import Any

from app.security.effective_authority import EffectiveAuthority
from app.services import audit_service, connector_service


@dataclass(frozen=True)
class ConnectorActionOutcome:
    binding: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    status_code: int = 200

    @property
    def ok(self) -> bool:
        return self.error_code is None


def run_connector_action(
    binding_id: str,
    action: str,
    params: dict[str, Any],
    authority: EffectiveAuthority,
) -> ConnectorActionOutcome:
    """Authorize, execute, and audit one binding action for any transport."""
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return ConnectorActionOutcome(
            error_code="NOT_FOUND",
            error_message="Binding not found",
            status_code=404,
        )
    if not authority.can_binding_action(binding_id, action, scope=binding["scope"]):
        connector_service.audit_delegated_execution_denial(
            authority, binding_id, action
        )
        return ConnectorActionOutcome(
            binding=binding,
            error_code="SCOPE_DENIED",
            error_message="Access denied to this binding action",
            status_code=403,
        )
    if not binding.get("enabled"):
        return ConnectorActionOutcome(
            binding=binding,
            error_code="DISABLED",
            error_message="Binding is disabled",
        )
    connector_type = connector_service.get_connector_type(binding["connector_type_id"])
    if not connector_type:
        return ConnectorActionOutcome(
            binding=binding,
            error_code="NOT_FOUND",
            error_message="Connector type not found",
            status_code=404,
        )

    result = connector_service.execute_authorized_binding_action_with_logging(
        binding_id, action, params, authority
    )
    audit_service.write_event(
        actor_type=authority.actor_type,
        actor_id=authority.actor_id,
        action="connector_action_executed",
        resource_type="connector_binding",
        resource_id=binding_id,
        result="success" if result.get("success") else "failure",
        details={
            "connector_type_id": binding["connector_type_id"],
            "action": action,
            "duration_ms": result.get("duration_ms"),
            "transport": result.get("transport"),
            **authority.safe_attribution(),
        },
    )
    return ConnectorActionOutcome(binding=binding, result=result)
