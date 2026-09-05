"""Public, version-matched setup guidance for Agent Core installations."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.branding import APP_NAME
from app.security.public_url import public_base_url

router = APIRouter(tags=["guidance"])


def _mcp_tools() -> list[str]:
    from app.routes.mcp import MANIFEST
    return [tool["name"] for tool in MANIFEST["tools"]]


def _setup_markdown(request: Request) -> str:
    base_url = public_base_url(request)
    return f"""# {APP_NAME} setup

Connect an MCP client to `{base_url}/mcp` with a bearer API key. Create the key from the dashboard's Integrations page. Keep it in the client's secret store, never in a prompt or project file.

The server filters tools by the agent's standing capabilities. Capability changes apply to the next request. Reconnect the MCP client to refresh any cached tool list.

Read `{base_url}/llms.txt` for a compact service overview. Playbooks live at `{base_url}/skills/`.
"""


@router.get("/llms.txt", response_class=PlainTextResponse)
def llms(request: Request):
    base_url = public_base_url(request)
    tools = ", ".join(f"`{name}`" for name in _mcp_tools())
    return (
        f"# {APP_NAME}\n\n"
        f"{APP_NAME} is a scoped memory, activity, credential-reference, and connector control service for agents.\n\n"
        f"MCP endpoint: {base_url}/mcp\n"
        f"Setup guidance: {base_url}/setup.md\n"
        f"Skill playbooks: {base_url}/skills/\n\n"
        f"Available MCP tools in this release: {tools}.\n"
    )


@router.get("/setup.md", response_class=PlainTextResponse)
def setup_markdown(request: Request):
    return _setup_markdown(request)


@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request):
    markdown = _setup_markdown(request)
    return HTMLResponse(
        "<html><head><title>Agent Core setup</title></head><body><pre>"
        + markdown.replace("&", "&amp;").replace("<", "&lt;")
        + "</pre></body></html>"
    )


_SKILLS_DIR = Path(__file__).resolve().parent.parent / "agent_skills"


def _skill_names() -> list[str]:
    return sorted(path.parent.name for path in _SKILLS_DIR.glob("*/SKILL.md"))


def _skill_content(name: str) -> str | None:
    if name not in _skill_names():
        return None
    return (_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


@router.get("/skills", response_class=PlainTextResponse)
def skills(request: Request):
    base_url = public_base_url(request)
    return "\n".join(f"- {name}: {base_url}/skills/{name}" for name in _skill_names()) + "\n"


@router.get("/skills/{name}", response_class=PlainTextResponse)
def skill(name: str):
    content = _skill_content(name)
    if content is None:
        return PlainTextResponse("Skill not found\n", status_code=404)
    return content
