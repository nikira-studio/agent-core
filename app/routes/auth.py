import secrets
from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel

from app.services import audit_service
from app.services.auth_service import (
    create_user,
    get_user_by_email,
    count_users,
    create_session,
    get_session,
    validate_session,
    update_session_activity,
    delete_session,
    enroll_otp,
    confirm_otp_enrollment,
    verify_otp,
    verify_otp_or_backup_code,
    is_otp_enrolled,
    get_user_by_id,
    verify_password,
    change_password,
    regenerate_backup_codes,
    delete_user,
    update_user,
)
from app.security.response_helpers import success_response, error_response, rate_limited_response
from app.security.rate_limiter import RL
from app.models.enums import normalize_id
from app.config import settings
from app.time_utils import parse_utc_datetime, utc_now


router = APIRouter(prefix="/api/auth", tags=["auth"])


def get_client_ip(request: Request) -> str:
    if request.client and request.client.host in settings.trusted_proxy_list:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_session_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    token = request.cookies.get("session_token")
    if token:
        return token
    return ""


def set_session_cookie(response, session_id: str) -> None:
    response.set_cookie(
        key="session_token",
        value=session_id,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class OtpVerifyRequest(BaseModel):
    session_id: str
    otp_code: str


class OtpConfirmRequest(BaseModel):
    otp_code: str


class OtpEnrollRequest(BaseModel):
    current_password: str
    otp_code: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def build_otp_qr_data_uri(otp_uri: str) -> str:
    try:
        import base64
        import io
        import qrcode
        from qrcode.image.svg import SvgPathImage

        image = qrcode.make(otp_uri, image_factory=SvgPathImage)
        buffer = io.BytesIO()
        image.save(buffer)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"
    except Exception:
        return ""


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str
    display_name: str
    role: str = "user"


class AdminUpdateUserRequest(BaseModel):
    email: str | None = None
    display_name: str | None = None
    role: str | None = None
    password: str | None = None


@router.post("/register")
async def register(body: RegisterRequest, request: Request):
    if len(body.password) < 8:
        return error_response("INVALID_PASSWORD", "Password must be at least 8 characters long", 400)
    if "@" not in body.email or "." not in body.email:
        return error_response("INVALID_EMAIL", "Invalid email format", 400)

    user_count = count_users()
    if user_count > 0:
        return error_response("REGISTRATION_DISABLED", "User registration is disabled", 403)

    try:
        user_id = normalize_id(body.email.split("@")[0])
    except ValueError:
        user_id = f"admin_{secrets.token_hex(4)}"

    role = "admin" if user_count == 0 else "user"

    try:
        user = create_user(
            user_id=user_id,
            email=body.email,
            password=body.password,
            display_name=body.display_name,
            role=role,
        )
    except Exception:
        return error_response("USER_EXISTS", "User could not be created", 400)

    audit_service.write_event(
        actor_type="user",
        actor_id=user_id,
        action="user_registered",
        result="success",
        ip_address=get_client_ip(request),
    )

    response = success_response({"user_id": user["id"], "role": user["role"]})
    if user_count == 0:
        session = create_session(user["id"], channel="dashboard", expiry_hours=settings.SESSION_DURATION_HOURS)
        set_session_cookie(response, session["session_id"])
    return response


@router.post("/login")
async def login(body: LoginRequest, request: Request):
    user = get_user_by_email(body.email)
    if not user:
        audit_service.write_event(
            actor_type="user",
            actor_id="unknown",
            action="session_login",
            result="failure",
            details={"reason": "user_not_found"},
            ip_address=get_client_ip(request),
        )
        return error_response("INVALID_CREDENTIALS", "Invalid credentials", 401)

    from app.services.auth_service import verify_password
    if not verify_password(body.password, user["password_hash"]):
        allowed, info = RL.check("user", user["id"], "login_failed")
        if not allowed:
            return rate_limited_response("RATE_LIMITED", "Too many failed login attempts", **info)
        audit_service.write_event(
            actor_type="user",
            actor_id=user["id"],
            action="session_login",
            result="failure",
            details={"reason": "bad_password"},
            ip_address=get_client_ip(request),
        )
        return error_response("INVALID_CREDENTIALS", "Invalid credentials", 401)

    otp_required = is_otp_enrolled(user["id"])
    channel = "pending_otp" if otp_required else "dashboard"
    session = create_session(user["id"], channel=channel, expiry_hours=settings.SESSION_DURATION_HOURS)

    audit_service.write_event(
        actor_type="user",
        actor_id=user["id"],
        action="session_login",
        result="success",
        ip_address=get_client_ip(request),
    )

    response = success_response({
        "session_id": session["session_id"],
        "requires_otp": otp_required,
        "user_id": user["id"],
    })
    
    if not otp_required:
        set_session_cookie(response, session["session_id"])
    return response


@router.post("/otp/enroll")
async def otp_enroll(
    body: OtpEnrollRequest,
    request: Request,
    session_token: str = Depends(get_session_token),
):
    session = validate_session(session_token)
    if not session:
        return error_response("UNAUTHORIZED", "Invalid or expired session", 401)

    user = get_user_by_email(session["email"])
    if not user or not verify_password(body.current_password, user["password_hash"]):
        return error_response("INVALID_PASSWORD", "Current password is incorrect", 403)

    if is_otp_enrolled(session["user_id"]):
        if not body.otp_code:
            return error_response("OTP_REQUIRED", "Current OTP code is required to reset OTP", 400)
        if not verify_otp(session["user_id"], body.otp_code):
            return error_response("INVALID_OTP", "Invalid OTP code", 403)

    otp_data = enroll_otp(session["user_id"])

    return success_response({
        "secret": otp_data["secret"],
        "otp_uri": otp_data["otp_uri"],
        "qr_svg": build_otp_qr_data_uri(otp_data["otp_uri"]),
    })


@router.post("/otp/confirm")
async def otp_confirm(
    body: OtpConfirmRequest,
    request: Request,
    session_token: str = Depends(get_session_token),
):
    session = validate_session(session_token)
    if not session:
        return error_response("UNAUTHORIZED", "Invalid or expired session", 401)

    backup_codes = confirm_otp_enrollment(session["user_id"], body.otp_code)
    if backup_codes is None:
        return error_response("INVALID_OTP", "Invalid OTP code", 401)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="otp_enrolled",
        result="success",
        ip_address=get_client_ip(request),
    )
    return success_response({"backup_codes": backup_codes})


@router.post("/otp/verify")
async def otp_verify(body: OtpVerifyRequest, request: Request):
    from app.services.auth_service import decode_jwt
    db_session_id = decode_jwt(body.session_id)
    if not db_session_id:
        return error_response("INVALID_SESSION", "Invalid session", 401)

    session = get_session(db_session_id)
    if not session:
        return error_response("INVALID_SESSION", "Invalid session", 401)

    if session.get("channel") != "pending_otp":
        return error_response("INVALID_SESSION", "Session is not pending OTP", 401)

    expires_at = parse_utc_datetime(session["expires_at"])
    if utc_now() > expires_at:
        delete_session(db_session_id)
        return error_response("INVALID_SESSION", "Session expired", 401)

    last_activity = parse_utc_datetime(session["last_activity"])
    if (utc_now() - last_activity).total_seconds() > (settings.INACTIVITY_TIMEOUT_MINUTES * 60):
        delete_session(db_session_id)
        return error_response("INVALID_SESSION", "Session timed out", 401)

    if is_otp_enrolled(session["user_id"]):
        is_valid = verify_otp_or_backup_code(session["user_id"], body.otp_code)
            
        if not is_valid:
            allowed, info = RL.check("user", session["user_id"], "otp_failed")
            if not allowed:
                return rate_limited_response("RATE_LIMITED", "Too many failed OTP attempts", **info)
            audit_service.write_event(
                actor_type="user",
                actor_id=session["user_id"],
                action="session_login",
                result="failure",
                details={"reason": "bad_otp"},
                ip_address=get_client_ip(request),
            )
            return error_response("INVALID_OTP", "Invalid OTP code", 401)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="session_login",
        result="success",
        ip_address=get_client_ip(request),
    )

    from app.database import get_db
    with get_db() as conn:
        conn.execute("UPDATE sessions SET channel = 'dashboard' WHERE id = ?", (db_session_id,))
        conn.commit()

    update_session_activity(db_session_id)

    response = success_response({
        "session_token": body.session_id,
        "user_id": session["user_id"],
    })
    set_session_cookie(response, body.session_id)
    return response


@router.post("/logout")
async def logout(request: Request, session_token: str = Depends(get_session_token)):
    session = validate_session(session_token)
    if session:
        audit_service.write_event(
            actor_type="user",
            actor_id=session["user_id"],
            action="session_logout",
            result="success",
            ip_address=get_client_ip(request),
        )
        from app.services.auth_service import decode_jwt
        db_session_id = decode_jwt(session_token)
        if db_session_id:
            delete_session(db_session_id)

    response = success_response({"message": "Logged out"})
    response.delete_cookie("session_token", path="/")
    return response


@router.post("/password")
async def change_user_password(
    body: ChangePasswordRequest,
    request: Request,
    session_token: str = Depends(get_session_token),
):
    session = validate_session(session_token)
    if not session:
        return error_response("UNAUTHORIZED", "Invalid or expired session", 401)

    if len(body.new_password) < 8:
        return error_response("INVALID_PASSWORD", "New password must be at least 8 characters", 400)

    ok, msg = change_password(session["user_id"], body.current_password, body.new_password)
    if not ok:
        return error_response("PASSWORD_CHANGE_FAILED", msg, 400)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="password_change",
        result="success",
        ip_address=get_client_ip(request),
    )
    return success_response({"message": "Password updated"})


@router.post("/otp/backup-codes")
async def regenerate_backup_codes_endpoint(
    request: Request,
    session_token: str = Depends(get_session_token),
):
    session = validate_session(session_token)
    if not session:
        return error_response("UNAUTHORIZED", "Invalid or expired session", 401)

    if not is_otp_enrolled(session["user_id"]):
        return error_response("NOT_ENROLLED", "OTP is not enrolled", 400)

    codes = regenerate_backup_codes(session["user_id"])
    return success_response({
        "backup_codes": codes,
        "warning": "Store these securely. They will not be shown again.",
    })


@router.post("/users")
async def create_user_endpoint(
    body: AdminCreateUserRequest,
    request: Request,
    session_token: str = Depends(get_session_token),
):
    session = validate_session(session_token)
    if not session:
        return error_response("UNAUTHORIZED", "Invalid or expired session", 401)
    if session.get("role") != "admin":
        return error_response("FORBIDDEN", "Admin access required", 403)
    if len(body.password) < 8:
        return error_response("INVALID_PASSWORD", "Password must be at least 8 characters long", 400)
    if "@" not in body.email or "." not in body.email:
        return error_response("INVALID_EMAIL", "Invalid email format", 400)
    if body.role not in ("admin", "user"):
        return error_response("INVALID_ROLE", "Role must be admin or user", 400)

    try:
        user_id = normalize_id(body.email.split("@")[0])
    except ValueError:
        user_id = f"user_{secrets.token_hex(4)}"

    suffix = 1
    base_user_id = user_id
    while get_user_by_id(user_id):
        suffix += 1
        user_id = f"{base_user_id}_{suffix}"

    try:
        user = create_user(user_id, body.email, body.password, body.display_name, body.role)
    except Exception:
        return error_response("USER_EXISTS", "User could not be created", 400)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="user_created",
        resource_type="user",
        resource_id=user["id"],
        result="success",
        ip_address=get_client_ip(request),
    )
    return success_response({"user": user}, status_code=201)


@router.put("/users/{user_id}")
async def update_user_endpoint(
    user_id: str,
    body: AdminUpdateUserRequest,
    request: Request,
    session_token: str = Depends(get_session_token),
):
    session = validate_session(session_token)
    if not session:
        return error_response("UNAUTHORIZED", "Invalid or expired session", 401)
    if session.get("role") != "admin":
        return error_response("FORBIDDEN", "Admin access required", 403)
    if body.password is not None and body.password != "" and len(body.password) < 8:
        return error_response("INVALID_PASSWORD", "Password must be at least 8 characters long", 400)
    if body.email is not None and ("@" not in body.email or "." not in body.email):
        return error_response("INVALID_EMAIL", "Invalid email format", 400)
    if body.role is not None and body.role not in ("admin", "user"):
        return error_response("INVALID_ROLE", "Role must be admin or user", 400)
    if user_id == session["user_id"] and body.role is not None and body.role != "admin":
        return error_response("FORBIDDEN", "You cannot remove admin from your current session", 400)

    ok, reason = update_user(
        user_id,
        email=body.email,
        display_name=body.display_name,
        role=body.role,
        password=body.password or None,
    )
    if not ok and reason:
        return error_response("UPDATE_FAILED", reason, 400)
    if not ok:
        return error_response("NOT_FOUND", "User not found", 404)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="user_updated",
        resource_type="user",
        resource_id=user_id,
        result="success",
        ip_address=get_client_ip(request),
    )
    return success_response({"message": "User updated"})


@router.delete("/users/{user_id}")
async def delete_user_endpoint(
    user_id: str,
    request: Request,
    session_token: str = Depends(get_session_token),
):
    session = validate_session(session_token)
    if not session:
        return error_response("UNAUTHORIZED", "Invalid or expired session", 401)
    if session.get("role") != "admin":
        return error_response("FORBIDDEN", "Admin access required", 403)
    if user_id == session["user_id"]:
        return error_response("FORBIDDEN", "Cannot delete your own account", 400)

    ok, reason = delete_user(user_id)
    if not ok and reason:
        return error_response("CONFLICT", reason, 409)
    if not ok:
        return error_response("NOT_FOUND", "User not found", 404)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="user_deleted",
        resource_type="user",
        resource_id=user_id,
        result="success",
        ip_address=get_client_ip(request),
    )
    return success_response({"message": "User deleted"})
