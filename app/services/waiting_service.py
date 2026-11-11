"""What is waiting on a person: one answer for the Overview, the Activity page and `attention_list`.

These used to be put together in three places that disagreed: Overview showed approvals, handoffs,
credential requests and memory suggestions, while `attention_list` (read by coordinators that tell a
person what is waiting) returned only handoffs and approvals. Each group is read here, once.

Groups, in the order a person should look at them:

- ``approvals``: requests an agent made that need a decision.
- ``handoffs``: ``needs_human`` requests agents left in a handoff.
- ``credential_requests``: secrets an agent asked a person to supply.
- ``memory_suggestions``: Memory Clean-up suggestions waiting for an answer (a count and a short list;
  never the memory text).
- ``stalled_tasks``: stale or blocked tasks (only when asked for; the Overview shows them).

``scopes`` is the set of scopes the viewer may read, ``None`` meaning every scope (an admin). A viewer
without a group in ``include`` simply gets an empty one.
"""

from __future__ import annotations

from typing import Optional

ALL_GROUPS = ("approvals", "handoffs", "credential_requests", "memory_suggestions")
SUGGESTION_LIST_LIMIT = 10


def _in_scopes(scope: Optional[str], scopes: Optional[list[str]]) -> bool:
    return scopes is None or scope in scopes


def waiting_on_person(
    *,
    scopes: Optional[list[str]] = None,
    user_id: Optional[str] = None,
    include: tuple[str, ...] = ALL_GROUPS,
    without_task_ref: bool = False,
    limit: int = 50,
) -> dict:
    """Every group named in ``include``, limited to ``scopes`` (and to ``user_id``'s handoffs when given)."""
    from app.services import (
        activity_service,
        approval_service,
        credential_request_service,
        memory_proposal_service,
    )

    limit = max(0, min(int(limit), 200))
    result: dict = {group: [] for group in ALL_GROUPS}
    result["memory_suggestions"] = {"count": 0, "items": []}

    if "approvals" in include:
        result["approvals"] = approval_service.list_for_scopes(scopes, "pending", limit or 1)
    if "handoffs" in include:
        result["handoffs"] = activity_service.list_needs_human(
            user_id=user_id, authorized_scopes=scopes, without_task_ref=without_task_ref, limit=limit
        )
    if "credential_requests" in include:
        result["credential_requests"] = [
            credential_request_service.public_view(r)
            for r in credential_request_service.list_pending()
            if _in_scopes(r.get("scope"), scopes)
        ][:limit]
    if "memory_suggestions" in include:
        result["memory_suggestions"] = memory_proposal_service.pending_summary(scopes, SUGGESTION_LIST_LIMIT)
    if "stalled_tasks" in include:
        result["stalled_tasks"] = activity_service.list_attention_activities(user_id=user_id, limit=8)
    return result
