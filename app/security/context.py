from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RequestContext:
    actor_type: str
    actor_id: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    read_scopes: list[str] = field(default_factory=list)
    write_scopes: list[str] = field(default_factory=list)
    active_workspace_ids: frozenset[str] = field(default_factory=frozenset)
    is_admin: bool = False


def build_user_context(session: dict) -> RequestContext:
    return RequestContext(
        actor_type="user",
        actor_id=session["user_id"],
        user_id=session["user_id"],
        is_admin=session.get("role") == "admin",
        read_scopes=[f"user:{session['user_id']}"],
        write_scopes=[f"user:{session['user_id']}"],
    )
