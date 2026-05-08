from app.routes.health import router as health_router
from app.routes.spec import router as spec_router
from app.routes.auth import router as auth_router
from app.routes.agents import router as agents_router
from app.routes.workspaces import router as workspaces_router
from app.routes.vault import router as vault_router
from app.routes.internal import router as internal_router
from app.routes.memory import router as memory_router
from app.routes.activity import router as activity_router
from app.routes.briefings import router as briefings_router
from app.routes.mcp import router as mcp_router
from app.routes.dashboard import router as dashboard_router
from app.routes.dashboard_api import router as dashboard_api_router
from app.routes.backup import router as backup_router
from app.routes.connectors import router as connector_router
from app.routes.connectors_page import router as connectors_page_router

__all__ = [
    "health_router",
    "spec_router",
    "auth_router",
    "agents_router",
    "workspaces_router",
    "vault_router",
    "internal_router",
    "memory_router",
    "activity_router",
    "briefings_router",
    "mcp_router",
    "dashboard_router",
    "dashboard_api_router",
    "backup_router",
    "connector_router",
    "connectors_page_router",
]
