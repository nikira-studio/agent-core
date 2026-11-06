import asyncio
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from starlette.background import BackgroundTask
from app.branding import APP_SLUG, APP_VERSION, MANIFEST_VERSION_KEY
from app.services import backup_service
from app.security.dependencies import require_admin, get_current_session
from app.security.response_helpers import success_response, error_response
from app.config import settings
from app.time_utils import utc_now


router = APIRouter(prefix="/api/backup", tags=["backup"])

# An export is made now and downloaded in a second request, so the one-time backup key
# travels in a response body (not a header, which proxies and logs keep) and the file
# is handed over once, to the admin who asked, within a few minutes.
DOWNLOAD_TTL_SECONDS = 600


def _downloads_dir() -> Path:
    path = settings.data_dir / "backup-downloads"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _sweep_downloads(directory: Path) -> None:
    now = time.time()
    for meta in directory.glob("*.json"):
        try:
            expired = json.loads(meta.read_text())["expires_at"] < now
        except Exception:
            expired = True
        if expired:
            for leftover in (meta, meta.with_suffix(".enc"), meta.with_suffix(".sending")):
                leftover.unlink(missing_ok=True)


@router.post("/export")
async def backup_export(
    request: Request,
    session: dict = Depends(require_admin),
):
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="backup_export",
        result="success",
        ip_address=get_client_ip(request),
    )

    zip_buf, backup_key = await asyncio.to_thread(
        backup_service.build_encrypted_backup_package,
        str(settings.db_path),
        str(settings.credential_key_path),
        session["user_id"],
        app_version=APP_VERSION,
    )

    filename = f"{APP_SLUG}-backup-{utc_now().strftime('%Y%m%d-%H%M%S')}.zip.enc"
    directory = _downloads_dir()
    await asyncio.to_thread(_sweep_downloads, directory)
    download_id = secrets.token_urlsafe(32)
    data_path = directory / f"{download_id}.enc"
    data_path.write_bytes(zip_buf.getvalue())
    os.chmod(data_path, 0o600)
    expires_at = time.time() + DOWNLOAD_TTL_SECONDS
    meta_path = directory / f"{download_id}.json"
    meta_path.write_text(json.dumps({"user_id": session["user_id"], "filename": filename, "expires_at": expires_at}))
    os.chmod(meta_path, 0o600)
    return success_response({
        "download_path": f"/api/backup/download/{download_id}",
        "filename": filename,
        "backup_key": backup_key.decode(),
        "expires_in_seconds": DOWNLOAD_TTL_SECONDS,
        "note": "Keep the backup key. It is shown here once and the file cannot be restored without it.",
    })


@router.get("/download/{download_id}")
async def backup_download(download_id: str, session: dict = Depends(require_admin)):
    """The encrypted backup made by a recent export: once, for the admin who made it."""
    if not download_id or not all(c.isalnum() or c in "-_" for c in download_id):
        return error_response("NOT_FOUND", "No such download", 404)
    directory = _downloads_dir()
    await asyncio.to_thread(_sweep_downloads, directory)
    meta_path = directory / f"{download_id}.json"
    data_path = directory / f"{download_id}.enc"
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return error_response("NOT_FOUND", "No such download, or it has expired. Export again.", 404)
    if meta.get("user_id") != session["user_id"]:
        return error_response("NOT_FOUND", "No such download, or it has expired. Export again.", 404)
    sending = data_path.with_suffix(".sending")
    try:
        os.rename(data_path, sending)  # claims it: a second request finds nothing
    except FileNotFoundError:
        return error_response("NOT_FOUND", "That download was already taken. Export again.", 404)
    meta_path.unlink(missing_ok=True)
    return FileResponse(
        sending,
        media_type="application/octet-stream",
        filename=meta["filename"],
        background=BackgroundTask(lambda: sending.unlink(missing_ok=True)),
    )


@router.post("/restore")
async def backup_restore(
    request: Request,
    session: dict = Depends(require_admin),
):
    form = await request.form()
    zip_file = form.get("backup")
    backup_key = form.get("backup_key", "").strip()
    mode = form.get("mode", "replace_all")

    # Restore always replaces the whole installation. Refusing any other mode
    # keeps a client that still asks for a partial restore from getting a full
    # replace it did not expect.
    if mode != "replace_all":
        return error_response("INVALID_MODE", "mode must be 'replace_all'", 400)

    if not zip_file:
        return error_response("MISSING_FILE", "No backup file provided", 400)

    contents = await zip_file.read()

    from io import BytesIO

    if not backup_key:
        return error_response(
            "MISSING_BACKUP_KEY",
            "This backup is encrypted. Enter the backup key shown at export time.",
            400,
        )

    try:
        # Fernet over the whole archive: proportional to the database, and
        # squarely on the loop until it finishes.
        contents = (
            await asyncio.to_thread(
                backup_service.decrypt_backup_package,
                contents,
                backup_key.encode(),
            )
        ).getvalue()
    except Exception:
        return error_response("INVALID_BACKUP_KEY", "Invalid backup key", 400)

    ok, msg, manifest = await asyncio.to_thread(
        backup_service.restore_from_zip,
        BytesIO(contents),
        str(settings.db_path),
        str(settings.credential_key_path),
    )

    if not ok:
        return error_response("RESTORE_FAILED", msg, 400)

    from app.services import audit_service
    from app.routes.auth import get_client_ip

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="backup_restore",
        result="success",
        details={
            "exported_by": manifest.get("exported_by"),
            "exported_at": manifest.get("exported_at"),
            "mode": mode,
        },
        ip_address=get_client_ip(request),
    )

    payload = {
        "message": "Restore complete",
        "mode": mode,
        "manifest": {
            "exported_by": manifest.get("exported_by"),
            "exported_at": manifest.get("exported_at"),
            MANIFEST_VERSION_KEY: manifest.get(MANIFEST_VERSION_KEY),
        },
    }
    return success_response(payload)


@router.get("/export/memory")
async def export_memory(
    fmt: str = "jsonl",
    session: dict = Depends(get_current_session),
):
    user_id = session["user_id"]
    if session.get("role") == "admin":
        user_id = None

    if fmt == "csv":
        buf = await asyncio.to_thread(
            backup_service.export_memory_csv,
            user_id=user_id
        )
        return StreamingResponse(
            buf,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=memory-export.csv"},
        )
    else:
        buf = await asyncio.to_thread(
            backup_service.export_memory_jsonl,
            user_id=user_id
        )
        return StreamingResponse(
            buf,
            media_type="application/jsonl",
            headers={"Content-Disposition": "attachment; filename=memory-export.jsonl"},
        )


@router.get("/export/credentials")
async def export_credentials_metadata(
    session: dict = Depends(get_current_session),
):
    user_id = session["user_id"]
    if session.get("role") == "admin":
        user_id = None

    buf = await asyncio.to_thread(
        backup_service.export_credentials_metadata,
        user_id=user_id
    )
    return StreamingResponse(
        buf,
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=credentials-metadata.json"
        },
    )


@router.get("/export/audit")
async def export_audit(
    fmt: str = "csv",
    actor_type: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    session: dict = Depends(require_admin),
):
    if fmt == "csv":
        buf = await asyncio.to_thread(
            backup_service.export_audit_csv,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
        )
        return StreamingResponse(
            buf,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit-export.csv"},
        )
    else:
        return error_response(
            "INVALID_FORMAT", "Only CSV format is supported for audit export", 400
        )


@router.get("/startup-checks")
async def startup_checks(session: dict = Depends(require_admin)):
    issues = await asyncio.to_thread(backup_service.run_startup_checks)
    all_ok = all(i["status"] == "OK" for i in issues)
    return JSONResponse(
        {
            "ok": all_ok,
            "data": {"checks": issues, "all_ok": all_ok},
        }
    )


@router.post("/maintenance")
async def run_maintenance(session: dict = Depends(require_admin)):
    result = await asyncio.to_thread(backup_service.run_scheduled_maintenance)
    return success_response(result)


@router.get("/maintenance/status")
def maintenance_status(session: dict = Depends(require_admin)):
    return success_response(backup_service.get_maintenance_status())
