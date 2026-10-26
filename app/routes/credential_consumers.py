"""Owner grant administration and an individually authenticated machine resolver."""
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from starlette.concurrency import run_in_threadpool

from app.security.dependencies import security, get_current_agent, require_admin
from app.security.exceptions import APIError
from app.security.response_helpers import success_response_with_headers
from app.services import credential_consumer_service as service
from app.database import get_db

router = APIRouter(prefix="/api/credential-consumers", tags=["credential consumers"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consumer_agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    entry_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expires_at: str | None = None
    allow_write: StrictBool = False


class LeaseStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_ids: list[StrictStr] = Field(min_length=1, max_length=64)


@router.post("/grants")
def create_grant(body: GrantRequest, owner=Depends(require_admin)):
    try:
        data = service.create_grant(body.consumer_agent_id, body.entry_id, owner["user_id"], body.expires_at, body.allow_write)
    except (ValueError, TypeError):
        raise APIError("INVALID_EXPIRY", "Grant expiry must be a valid timestamp", 400)
    return success_response_with_headers({"grant": data}, NO_STORE, status_code=201)


@router.get("/grants")
def list_grants(consumer_agent_id: str, owner=Depends(require_admin)):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM credential_consumer_grants WHERE consumer_agent_id=? ORDER BY created_at LIMIT 1001", (consumer_agent_id,)).fetchall()
    return success_response_with_headers({"grants": [dict(r) for r in rows[:1000]], "truncated": len(rows) > 1000}, NO_STORE)


@router.delete("/grants/{grant_id}")
def revoke_grant(grant_id: str, owner=Depends(require_admin)):
    return success_response_with_headers(service.revoke_grant(grant_id, owner["user_id"]), NO_STORE)


def _resolve(request, credentials, agent, entry_id, metadata_only):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", entry_id):
        raise APIError("INVALID_ENTRY", "Invalid entry identifier", 400)
    if request.url.scheme != "https":
        raise APIError("HTTPS_REQUIRED", "Credential resolution requires HTTPS", 403)
    return success_response_with_headers(service.resolve(credentials.credentials, entry_id, metadata_only), NO_STORE)


@router.post("/entries/{entry_id}/resolve")
def resolve(entry_id: str, request: Request, credentials: HTTPAuthorizationCredentials = Depends(security), agent=Depends(get_current_agent)):
    return _resolve(request, credentials, agent, entry_id, False)


@router.post("/entries/{entry_id}/status")
def status(entry_id: str, request: Request, credentials: HTTPAuthorizationCredentials = Depends(security), agent=Depends(get_current_agent)):
    return _resolve(request, credentials, agent, entry_id, True)


@router.post("/leases/status")
def lease_status(body: LeaseStatusRequest, request: Request, credentials: HTTPAuthorizationCredentials = Depends(security), agent=Depends(get_current_agent)):
    if request.url.scheme != "https":
        raise APIError("HTTPS_REQUIRED", "Credential lease validation requires HTTPS", 403)
    import re
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", entry_id) for entry_id in body.entry_ids):
        raise APIError("INVALID_LEASE_SET", "Lease status entry IDs must be valid identifiers", 400)
    return success_response_with_headers(service.lease_status(credentials.credentials, body.entry_ids), NO_STORE)


@router.post("/entries/{entry_id}/write")
async def write(entry_id: str, request: Request, credentials: HTTPAuthorizationCredentials = Depends(security), agent=Depends(get_current_agent)):
    import json
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", entry_id):
        raise APIError("INVALID_ENTRY", "Invalid entry identifier", 400)
    if request.url.scheme != "https":
        raise APIError("WRITE_DENIED", "Credential writes require direct HTTPS consumer authority", 403)
    # Parse the secret-bearing boundary manually: validation responses must not echo input.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > service.MAX_WRITE_BYTES * 6 + 2048:
            raise APIError("INVALID_WRITE", "Credential write request exceeds its size bound", 413)
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or set(data) != {"request_id", "expected_version", "value"}:
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError):
        raise APIError("INVALID_WRITE", "Invalid credential write request", 400)
    result = await run_in_threadpool(service.write, credentials.credentials, entry_id, data["request_id"], data["expected_version"], data["value"])
    return success_response_with_headers(result, NO_STORE, status_code=result["status_code"])
