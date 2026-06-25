import logging
import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.branding import APP_NAME, ENV_PREFIX
from app.config import settings
from app.routes import (
    health_router,
    spec_router,
    auth_router,
    agents_router,
    workspaces_router,
    credentials_router,
    internal_router,
    memory_router,
    activity_router,
    briefings_router,
    mcp_router,
    dashboard_router,
    dashboard_api_router,
    backup_router,
    connector_router,
    connector_types_router,
    connector_compat_router,
    connectors_page_router,
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
    events_router,
    webhooks_router,
)
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


def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version="1.0.0",
        description=f"{APP_NAME} local-first AI agent control layer",
    )

    @app.middleware("http")
    async def size_limit_middleware(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_SIZE:
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
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
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

    templates = Jinja2Templates(directory="app/dashboard/templates")
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory="app/dashboard/static"), name="static")

    app.include_router(health_router, tags=["health"])
    app.include_router(spec_router, tags=["spec"])
    app.include_router(auth_router, tags=["auth"])
    app.include_router(agents_router, tags=["agents"])
    app.include_router(workspaces_router, tags=["workspaces"])
    app.include_router(credentials_router, tags=["credentials"])
    app.include_router(internal_router, tags=["internal"])
    app.include_router(memory_router, tags=["memory"])
    app.include_router(activity_router, tags=["activity"])
    app.include_router(briefings_router, tags=["briefings"])
    app.include_router(mcp_router, tags=["mcp"])
    app.include_router(connector_router, tags=["connector_bindings"])
    app.include_router(connector_compat_router, tags=["connector_bindings"])
    app.include_router(connector_types_router, tags=["connector_types"])
    app.include_router(connectors_page_router, tags=["connectors_page"])
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
    app.include_router(dashboard_api_router, tags=["dashboard_api"])
    app.include_router(backup_router, tags=["backup"])
    app.include_router(events_router, tags=["events"])
    app.include_router(webhooks_router, tags=["webhooks"])
    app.include_router(dashboard_router, prefix="", tags=["dashboard"])

    settings.data_dir
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
