"""Public, non-sensitive discovery metadata for Agent Core clients."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.branding import APP_NAME
from app.security.public_url import public_base_url

router = APIRouter(tags=["discovery"])

DOCUMENTATION_URL = (
    "https://github.com/nikira-studio/agent-core/blob/main/docs/integrations.md"
)


@router.get("/.well-known/agent-core.json")
def agent_core_discovery(request: Request):
    """Describe the public MCP connection without disclosing installation state."""
    from app.routes.mcp import MANIFEST

    base_url = public_base_url(request)
    return JSONResponse(
        content={
            "name": APP_NAME,
            "version": MANIFEST["version"],
            "mcp_url": f"{base_url}/mcp",
            "transport": "streamable-http",
            "authentication": {"type": "bearer"},
            "documentation_url": DOCUMENTATION_URL,
            "setup_url": f"{base_url}/setup.md",
            "llms_url": f"{base_url}/llms.txt",
            "skills_url": f"{base_url}/skills",
        }
    )
