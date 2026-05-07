from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from typing import Optional

from app.services import audit_service, workspace_service
from app.security.dependencies import get_current_session
from app.security.response_helpers import success_response, error_response
from app.models.enums import normalize_id


router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class CreateWorkspaceRequest(BaseModel):
    id: str
    name: str
    description: Optional[str] = ""


class UpdateWorkspaceRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


def _can_manage_workspace(workspace: dict, session: dict) -> bool:
    return session.get("role") == "admin" or workspace.get("owner_user_id") == session["user_id"]


@router.get("")
async def list_workspaces(session: dict = Depends(get_current_session)):
    owner_user_id = None if session.get("role") == "admin" else session["user_id"]
    workspaces = workspace_service.list_workspaces(owner_user_id=owner_user_id)
    return success_response({"workspaces": workspaces})


@router.post("")
async def create_workspace(
    body: CreateWorkspaceRequest,
    session: dict = Depends(get_current_session),
):
    try:
        normalized_id = normalize_id(body.id)
    except ValueError as e:
        return error_response("INVALID_ID", str(e), 400)

    existing = workspace_service.get_workspace_by_id(normalized_id)
    if existing:
        return error_response("WORKSPACE_EXISTS", "Workspace ID already exists", 400)

    workspace = workspace_service.create_workspace(
        workspace_id=body.id,
        name=body.name,
        owner_user_id=session["user_id"],
        description=body.description or "",
    )

    return success_response({"workspace": workspace}, status_code=201)


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: str, session: dict = Depends(get_current_session)):
    workspace = workspace_service.get_workspace_by_id(workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)

    if workspace["owner_user_id"] != session["user_id"] and session.get("role") != "admin":
        return error_response("FORBIDDEN", "Access denied", 403)

    return success_response({"workspace": workspace})


@router.put("/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    body: UpdateWorkspaceRequest,
    session: dict = Depends(get_current_session),
):
    workspace = workspace_service.get_workspace_by_id(workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)

    if not _can_manage_workspace(workspace, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    workspace_service.update_workspace(
        workspace_id,
        name=body.name,
        description=body.description,
        is_active=body.is_active,
    )

    return success_response({"message": "Workspace updated"})


@router.delete("/{workspace_id}")
async def deactivate_workspace(workspace_id: str, session: dict = Depends(get_current_session)):
    workspace = workspace_service.get_workspace_by_id(workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)

    if not _can_manage_workspace(workspace, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    workspace_service.deactivate_workspace(workspace_id)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="workspace_deactivated",
        resource_type="workspace",
        resource_id=workspace_id,
        result="success",
    )

    return success_response({"message": "Workspace deactivated"})


@router.post("/{workspace_id}/activate")
async def activate_workspace(workspace_id: str, session: dict = Depends(get_current_session)):
    workspace = workspace_service.get_workspace_by_id(workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)

    if not _can_manage_workspace(workspace, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    workspace_service.reactivate_workspace(workspace_id)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="workspace_reactivated",
        resource_type="workspace",
        resource_id=workspace_id,
        result="success",
    )

    return success_response({"message": "Workspace reactivated"})


@router.post("/{workspace_id}/purge")
async def purge_workspace(workspace_id: str, session: dict = Depends(get_current_session)):
    workspace = workspace_service.get_workspace_by_id(workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)

    if not _can_manage_workspace(workspace, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    workspace_service.delete_workspace_hard(workspace_id)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="workspace_purged",
        resource_type="workspace",
        resource_id=workspace_id,
        result="success",
    )

    return success_response({"message": "Workspace permanently deleted"})
