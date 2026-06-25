from app.routes.health import router as health_router
from app.routes.spec import router as spec_router
from app.routes.auth import router as auth_router
from app.routes.agents import router as agents_router
from app.routes.workspaces import router as workspaces_router
from app.routes.credentials import router as credentials_router
from app.routes.internal import router as internal_router
from app.routes.memory import router as memory_router
from app.routes.activity import router as activity_router
from app.routes.briefings import router as briefings_router
from app.routes.mcp import router as mcp_router
from app.routes.dashboard import router as dashboard_router
from app.routes.dashboard_api import router as dashboard_api_router
from app.routes.backup import router as backup_router
from app.routes.connectors import router as connector_router
from app.routes.connectors import connector_types_router
from app.routes.connectors import connector_compat_router
from app.routes.connectors_page import router as connectors_page_router
from app.routes.users_page import router as users_page_router
from app.routes.activity_page import router as activity_page_router
from app.routes.audit_page import router as audit_page_router
from app.routes.agents_page import router as agents_page_router
from app.routes.workspaces_page import router as workspaces_page_router
from app.routes.memory_page import router as memory_page_router
from app.routes.overview_page import router as overview_page_router
from app.routes.auth_pages import router as auth_pages_router
from app.routes.settings_page import router as settings_page_router
from app.routes.webhooks_page import router as webhooks_page_router
from app.routes.events import router as events_router
from app.routes.webhooks import router as webhooks_router

__all__ = [
    "health_router",
    "spec_router",
    "auth_router",
    "agents_router",
    "workspaces_router",
    "credentials_router",
    "internal_router",
    "memory_router",
    "activity_router",
    "briefings_router",
    "mcp_router",
    "dashboard_router",
    "dashboard_api_router",
    "backup_router",
    "connector_router",
    "connector_types_router",
    "connector_compat_router",
    "connectors_page_router",
    "users_page_router",
    "activity_page_router",
    "audit_page_router",
    "agents_page_router",
    "workspaces_page_router",
    "memory_page_router",
    "overview_page_router",
    "auth_pages_router",
    "settings_page_router",
    "webhooks_page_router",
    "events_router",
    "webhooks_router",
]
