"""Public, non-sensitive discovery metadata for Agent Core clients."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.branding import APP_NAME

router = APIRouter(tags=["discovery"])

DOCUMENTATION_URL = (
    "https://github.com/nikira-studio/agent-core/blob/main/docs/integrations.md"
)


@router.get("/.well-known/agent-core.json")
def agent_core_discovery(request: Request):
    """Describe the public MCP connection without disclosing installation state."""
    from app.routes.mcp import MANIFEST

    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(
        content={
            "name": APP_NAME,
            "version": MANIFEST["version"],
            "mcp_url": f"{base_url}/mcp",
            "transport": "streamable-http",
            "authentication": {"type": "bearer"},
            "documentation_url": DOCUMENTATION_URL,
        }
    )
