from fastapi import APIRouter, Depends
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


class WorkspaceCollaboratorRequest(BaseModel):
    can_read: bool = True
    can_write: bool = False


def _can_manage_workspace(workspace: dict, session: dict) -> bool:
    return session.get("role") == "admin" or workspace.get("owner_user_id") == session["user_id"]


def _can_view_workspace(workspace: dict, session: dict) -> bool:
    if session.get("role") == "admin" or workspace.get("owner_user_id") == session["user_id"]:
        return True
    return workspace_service.can_user_read_workspace(session["user_id"], workspace["id"])


@router.get("")
async def list_workspaces(session: dict = Depends(get_current_session)):
    if session.get("role") == "admin":
        workspaces = workspace_service.list_workspaces()
    else:
        workspaces = workspace_service.list_accessible_workspaces(session["user_id"])
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

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="workspace_created",
        resource_type="workspace",
        resource_id=workspace["id"],
        result="success",
    )

    return success_response({"workspace": workspace}, status_code=201)


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: str, session: dict = Depends(get_current_session)):
    workspace = workspace_service.get_workspace_by_id(workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)

    if not _can_view_workspace(workspace, session):
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

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="workspace_updated",
        resource_type="workspace",
        resource_id=workspace_id,
        result="success",
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


@router.get("/{workspace_id}/collaborators")
async def list_collaborators(workspace_id: str, session: dict = Depends(get_current_session)):
    workspace = workspace_service.get_workspace_by_id(workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)
    if not _can_manage_workspace(workspace, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    return success_response(
        {"collaborators": workspace_service.list_workspace_collaborators(workspace_id)}
    )


@router.put("/{workspace_id}/collaborators/{user_id}")
async def upsert_collaborator(
    workspace_id: str,
    user_id: str,
    body: WorkspaceCollaboratorRequest,
    session: dict = Depends(get_current_session),
):
    workspace = workspace_service.get_workspace_by_id(workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)
    if not _can_manage_workspace(workspace, session):
        return error_response("FORBIDDEN", "Access denied", 403)
    workspace_service.upsert_workspace_collaborator(
        workspace_id=workspace_id,
        user_id=user_id,
        can_read=body.can_read,
        can_write=body.can_write,
        created_by=session["user_id"],
    )

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="workspace_collaborator_upserted",
        resource_type="workspace",
        resource_id=workspace_id,
        result="success",
        details={
            "user_id": user_id,
            "can_read": body.can_read,
            "can_write": body.can_write,
        },
    )
    return success_response({"message": "Collaborator updated"})


@router.delete("/{workspace_id}/collaborators/{user_id}")
async def remove_collaborator(
    workspace_id: str,
    user_id: str,
    session: dict = Depends(get_current_session),
):
    workspace = workspace_service.get_workspace_by_id(workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)
    if not _can_manage_workspace(workspace, session):
        return error_response("FORBIDDEN", "Access denied", 403)

    removed = workspace_service.remove_workspace_collaborator(workspace_id, user_id)
    if not removed:
        return error_response("NOT_FOUND", "Collaborator not found or is protected", 404)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="workspace_collaborator_removed",
        resource_type="workspace",
        resource_id=workspace_id,
        result="success",
        details={"user_id": user_id},
    )
    return success_response({"message": "Collaborator removed"})
