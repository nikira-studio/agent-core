from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse
from app.branding import APP_SLUG, APP_VERSION, BACKUP_KEY_HEADER, MANIFEST_VERSION_KEY
from app.services import backup_service
from app.security.dependencies import require_admin, get_current_session
from app.security.response_helpers import success_response, error_response
from app.config import settings
from app.time_utils import utc_now


router = APIRouter(prefix="/api/backup", tags=["backup"])


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

    zip_buf, backup_key = backup_service.build_encrypted_backup_package(
        str(settings.db_path),
        str(settings.credential_key_path),
        session["user_id"],
        app_version=APP_VERSION,
    )

    filename = f"{APP_SLUG}-backup-{utc_now().strftime('%Y%m%d-%H%M%S')}.zip.enc"

    return StreamingResponse(
        zip_buf,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            BACKUP_KEY_HEADER: backup_key.decode(),
            f"{BACKUP_KEY_HEADER}-Encrypted": "true",
        },
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

    if mode not in ("replace_all", "merge"):
        return error_response(
            "INVALID_MODE", "mode must be 'replace_all' or 'merge'", 400
        )

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
        contents = backup_service.decrypt_backup_package(
            contents,
            backup_key.encode(),
        ).getvalue()
    except Exception:
        return error_response("INVALID_BACKUP_KEY", "Invalid backup key", 400)

    if mode == "merge":
        ok, msg, manifest = backup_service.merge_restore_from_zip(
            BytesIO(contents),
            str(settings.db_path),
            str(settings.credential_key_path),
        )
    else:
        ok, msg, manifest = backup_service.restore_from_zip(
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

    return success_response(
        {
            "message": "Restore complete",
            "mode": mode,
            "manifest": {
                "exported_by": manifest.get("exported_by"),
                "exported_at": manifest.get("exported_at"),
                MANIFEST_VERSION_KEY: manifest.get(MANIFEST_VERSION_KEY),
            },
        }
    )


@router.get("/export/memory")
async def export_memory(
    fmt: str = "jsonl",
    session: dict = Depends(get_current_session),
):
    user_id = session["user_id"]
    if session.get("role") == "admin":
        user_id = None

    if fmt == "csv":
        buf = backup_service.export_memory_csv(user_id=user_id)
        return StreamingResponse(
            buf,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=memory-export.csv"},
        )
    else:
        buf = backup_service.export_memory_jsonl(user_id=user_id)
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

    buf = backup_service.export_credentials_metadata(user_id=user_id)
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
        buf = backup_service.export_audit_csv(
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
    issues = backup_service.run_startup_checks()
    all_ok = all(i["status"] == "OK" for i in issues)
    return JSONResponse(
        {
            "ok": all_ok,
            "data": {"checks": issues, "all_ok": all_ok},
        }
    )


@router.post("/maintenance")
async def run_maintenance(session: dict = Depends(require_admin)):
    result = backup_service.run_scheduled_maintenance()
    return success_response(result)
