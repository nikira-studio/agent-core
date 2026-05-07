from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.services import memory_service, vault_service, activity_service, audit_service, agent_service, workspace_service
from app.services.broker_service import rotate_broker_credential
from app.security.dependencies import require_admin, get_current_session
from app.security.response_helpers import success_response, error_response


router = APIRouter(prefix="/api/dashboard", tags=["dashboard_api"])


@router.get("/overview")
async def dashboard_overview(session: dict = Depends(require_admin)):
    agent_count = len(agent_service.list_agents())
    workspace_count = len(workspace_service.list_workspaces())
    recent_activity = activity_service.list_activities(limit=5)
    return success_response({
        "agent_count": agent_count,
        "workspace_count": workspace_count,
        "recent_activity": recent_activity,
    })


@router.get("/memory")
async def dashboard_memory(
    scope: str | None = None,
    memory_class: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: dict = Depends(get_current_session),
):
    effective_scope = f"user:{session['user_id']}"

    if scope:
        if session.get("role") == "admin":
            effective_scope = scope
        else:
            return error_response("SCOPE_DENIED", "Arbitrary scope access is restricted to admins", 403)

    records = memory_service.get_memory_by_scope(
        scope=effective_scope,
        limit=min(limit, 100),
        offset=offset,
        record_status="active",
    )
    return success_response({"records": records, "total": len(records)})


@router.get("/vault")
async def dashboard_vault(
    scope: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: dict = Depends(get_current_session),
):
    effective_scope = f"user:{session['user_id']}"

    if scope:
        if session.get("role") == "admin":
            effective_scope = scope
        else:
            return error_response("SCOPE_DENIED", "Arbitrary scope access is restricted to admins", 403)

    entries = vault_service.list_vault_entries(
        scope=effective_scope,
        limit=min(limit, 100),
        offset=offset,
    )
    masked = []
    for entry in entries:
        masked.append({
            "id": entry["id"],
            "scope": entry["scope"],
            "name": entry["name"],
            "label": entry.get("label"),
            "value_type": entry.get("value_type"),
            "reference_name": entry["reference_name"],
            "created_at": entry.get("created_at"),
        })
    return success_response({"entries": masked, "total": len(masked)})


@router.get("/audit")
async def dashboard_audit(
    actor_type: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
    session: dict = Depends(require_admin),
):
    events = audit_service.query_events(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        limit=min(limit, 500),
        offset=offset,
    )
    return success_response({"events": events, "total": len(events)})


@router.get("/activity")
async def dashboard_activity(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: dict = Depends(get_current_session),
):
    activity_service.mark_stale_activities()
    activities = activity_service.list_activities(
        user_id=session["user_id"],
        status=status,
        limit=min(limit, 100),
        offset=offset,
    )
    return success_response({"activities": activities, "total": len(activities)})


@router.get("/activity/summary")
async def dashboard_activity_summary(session: dict = Depends(get_current_session)):
    all_active = activity_service.list_activities(user_id=session["user_id"], status="active", limit=1000)
    all_stale = activity_service.list_activities(user_id=session["user_id"], status="stale", limit=1000)
    recent = activity_service.list_activities(user_id=session["user_id"], limit=10)
    return success_response({
        "active_count": len(all_active),
        "stale_count": len(all_stale),
        "recent": recent,
    })


@router.post("/broker/rotate")
async def rotate_broker(session: dict = Depends(require_admin)):
    new_credential = rotate_broker_credential()
    return success_response({
        "credential": new_credential,
        "message": "Store this credential securely. It will not be shown again.",
    })


@router.get("/audit/export")
async def export_audit_csv(
    actor_type: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    result: str | None = None,
    session: dict = Depends(require_admin),
):
    events = audit_service.query_events(
        actor_type=actor_type,
        action=action,
        resource_type=resource_type,
        result=result,
        limit=500,
        offset=0,
    )
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "actor_type", "actor_id", "action", "resource_type", "resource_id", "result", "ip_address", "details"])
    for e in events:
        writer.writerow([
            e.get("timestamp", ""),
            e.get("actor_type", ""),
            e.get("actor_id", ""),
            e.get("action", ""),
            e.get("resource_type", ""),
            e.get("resource_id", ""),
            e.get("result", ""),
            e.get("ip_address", ""),
            e.get("details", ""),
        ])
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-log.csv"},
    )
