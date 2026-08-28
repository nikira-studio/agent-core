import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.branding import APP_NAME, ENV_PREFIX
from app.config import settings
from app.routes import (
    health_router,
    spec_router,
    auth_router,
    delegations_router,
    delegation_requests_router,
    agents_router,
    workspaces_router,
    credentials_router,
    internal_router,
    memory_router,
    memory_proposals_router,
    activity_router,
    briefings_router,
    workspace_sync_router,
    discovery_router,
    mcp_router,
    integrations_page_router,
    dashboard_api_router,
    backup_router,
    connector_router,
    connector_types_router,
    connector_compat_router,
    connectors_page_router,
    credentials_page_router,
    users_page_router,
    activity_page_router,
    audit_page_router,
    agents_page_router,
    workspaces_page_router,
    memory_page_router,
    overview_page_router,
    auth_pages_router,
    settings_page_router,
    webhooks_page_router,
    delegation_requests_page_router,
    events_router,
    webhooks_router,
)
from app.database import DatabaseUnavailable
from app.security.exceptions import APIError
from app.services.broker_service import ensure_broker_credential
from app.database import init_db


class _SuppressManifestPolling(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("GET /mcp" in msg and '" 200' in msg)


logging.getLogger("uvicorn.access").addFilter(_SuppressManifestPolling())


ALLOWED_IPS: set = set()
_ip_list = os.environ.get(f"{ENV_PREFIX}ALLOWED_IPS", "").strip()
if _ip_list:
    ALLOWED_IPS = {ip.strip() for ip in _ip_list.split(",") if ip.strip()}

MAX_REQUEST_SIZE = 1024 * 1024


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from app.services.scheduler_service import (
        start_maintenance_scheduler,
        stop_maintenance_scheduler,
    )

    app.state.maintenance_task = start_maintenance_scheduler()
    try:
        yield
    finally:
        await stop_maintenance_scheduler(app.state.maintenance_task)


def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version="1.0.0",
        description=f"{APP_NAME} local-first AI agent control layer",
        lifespan=_lifespan,
    )

    @app.middleware("http")
    async def size_limit_middleware(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": {
                            "code": "INVALID_CONTENT_LENGTH",
                            "message": "Malformed Content-Length header",
                        },
                    },
                )
            if declared_size > MAX_REQUEST_SIZE:
                return JSONResponse(
                    status_code=413,
                    content={
                        "ok": False,
                        "error": {
                            "code": "PAYLOAD_TOO_LARGE",
                            "message": "Request body too large",
                        },
                    },
                )
        return await call_next(request)

    @app.middleware("http")
    async def reject_body_grant_credentials(request: Request, call_next):
        """Grant credentials are transport metadata, never model/body data."""
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            try:
                payload = await request.json()
            except Exception:
                payload = None
            forbidden = {
                "grant_secret", "grant_credential", "delegation_secret",
                "x-agent-core-grant",
            }

            def contains_forbidden(value) -> bool:
                if isinstance(value, dict):
                    return any(str(key).lower() in forbidden or contains_forbidden(item) for key, item in value.items())
                if isinstance(value, list):
                    return any(contains_forbidden(item) for item in value)
                return False

            if contains_forbidden(payload):
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": {"code": "GRANT_HEADER_REQUIRED", "message": "Delegated credentials are accepted only in the dedicated header"}},
                )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    if settings.SLOW_REQUEST_LOG_MS > 0:

        @app.middleware("http")
        async def slow_request_logging(request: Request, call_next):
            # The access log records completion, not duration, so a stalled
            # request is invisible after the fact. For streaming responses the
            # measured time is time-to-first-byte, which keeps long-lived SSE
            # connections from logging as slow.
            import time

            start = time.monotonic()
            response = await call_next(request)
            duration_ms = (time.monotonic() - start) * 1000
            if duration_ms >= settings.SLOW_REQUEST_LOG_MS:
                logging.getLogger("app.slow_requests").warning(
                    "Slow request: %s %s took %.0fms (status %s)",
                    request.method,
                    request.url.path,
                    duration_ms,
                    response.status_code,
                )
            return response

    if ALLOWED_IPS:

        @app.middleware("http")
        async def ip_allowlist(request: Request, call_next):
            if request.client and request.client.host:
                import ipaddress

                try:
                    remote_ip = ipaddress.ip_address(request.client.host)
                    allowed = False
                    for net_str in ALLOWED_IPS:
                        net = ipaddress.ip_network(net_str.strip(), strict=False)
                        if remote_ip in net:
                            allowed = True
                            break
                    if not allowed:
                        return JSONResponse(
                            status_code=403,
                            content={
                                "ok": False,
                                "error": {
                                    "code": "IP_BLOCKED",
                                    "message": "Your IP is not allowed",
                                },
                            },
                        )
                except Exception:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "ok": False,
                            "error": {
                                "code": "IP_BLOCKED",
                                "message": "Your IP is not allowed",
                            },
                        },
                    )
            return await call_next(request)

    _cors_origins = ["*"]
    _env_origins = os.environ.get(f"{ENV_PREFIX}CORS_ORIGINS", "").strip()
    if _env_origins:
        _cors_origins = [o.strip() for o in _env_origins.split(",") if o.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=bool(_env_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            content={"ok": False, "error": {"code": exc.code, "message": exc.message}},
            status_code=exc.status_code,
        )

    @app.exception_handler(DatabaseUnavailable)
    async def database_unavailable_handler(
        request: Request, exc: DatabaseUnavailable
    ) -> JSONResponse:
        """A restore is holding the database still.

        This is a real, brief, and self-resolving condition, so it is reported
        as one: 503 with a Retry-After, not the 500 an unhandled exception would
        produce. Clients that retry will succeed; clients that log will log
        something true.
        """
        return JSONResponse(
            content={
                "ok": False,
                "error": {"code": "DATABASE_UNAVAILABLE", "message": str(exc)},
            },
            status_code=503,
            headers={"Retry-After": "5"},
        )

    app.mount("/static", StaticFiles(directory="app/dashboard/static"), name="static")

    app.include_router(health_router, tags=["health"])
    app.include_router(spec_router, tags=["spec"])
    app.include_router(auth_router, tags=["auth"])
    app.include_router(delegations_router, tags=["delegations"])
    app.include_router(delegation_requests_router, tags=["delegations"])
    app.include_router(agents_router, tags=["agents"])
    app.include_router(workspaces_router, tags=["workspaces"])
    app.include_router(credentials_router, tags=["credentials"])
    app.include_router(internal_router, tags=["internal"])
    # Before memory_router: its GET /api/memory/{record_id} would otherwise
    # match /api/memory/proposals and look for a record called "proposals".
    app.include_router(memory_proposals_router, tags=["memory"])
    app.include_router(memory_router, tags=["memory"])
    app.include_router(activity_router, tags=["activity"])
    app.include_router(briefings_router, tags=["briefings"])
    app.include_router(workspace_sync_router, tags=["workspace_sync"])
    app.include_router(discovery_router, tags=["discovery"])
    app.include_router(mcp_router, tags=["mcp"])
    app.include_router(connector_router, tags=["connector_bindings"])
    app.include_router(connector_compat_router, tags=["connector_bindings"])
    app.include_router(connector_types_router, tags=["connector_types"])
    app.include_router(connectors_page_router, tags=["connectors_page"])
    app.include_router(credentials_page_router, tags=["credentials_page"])
    app.include_router(users_page_router, tags=["users_page"])
    app.include_router(activity_page_router, tags=["activity_page"])
    app.include_router(audit_page_router, tags=["audit_page"])
    app.include_router(agents_page_router, tags=["agents_page"])
    app.include_router(workspaces_page_router, tags=["workspaces_page"])
    app.include_router(memory_page_router, tags=["memory_page"])
    app.include_router(overview_page_router, tags=["overview_page"])
    app.include_router(auth_pages_router, tags=["auth_pages"])
    app.include_router(settings_page_router, tags=["settings_page"])
    app.include_router(webhooks_page_router, tags=["webhooks_page"])
    app.include_router(delegation_requests_page_router, tags=["delegation_requests_page"])
    app.include_router(dashboard_api_router, tags=["dashboard_api"])
    app.include_router(backup_router, tags=["backup"])
    app.include_router(events_router, tags=["events"])
    app.include_router(webhooks_router, tags=["webhooks"])
    app.include_router(integrations_page_router, prefix="", tags=["integrations_page"])

    # settings.data_dir creates the directory as a side effect of being read.
    # Calling mkdir explicitly says so, instead of relying on a bare attribute
    # access that reads like dead code to anyone tidying up.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    ensure_broker_credential()
    from app.connectors import generic_http  # noqa: F401 - registers Generic HTTP connector

    try:
        from app.services.adapter_loader import discover_and_seed_adapters

        discover_and_seed_adapters()
    except Exception as exc:
        logging.exception("Failed to restore installed adapters: %s", exc)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.PORT, reload=True)
