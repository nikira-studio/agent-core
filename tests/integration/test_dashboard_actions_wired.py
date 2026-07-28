"""Every action attribute a page renders must have a listener on that page.

Moving row actions out of inline handlers fixed a class of injection, but it
introduced one of its own: the markup and the behaviour now live in different
places, and nothing connects them. A button whose listener was added to a
different page's script looks completely normal and simply does nothing when
clicked — which is exactly what happened to "Verify and Enable OTP".
"""

import re

import pytest

# Pages that render row actions. Static-only pages are not interesting here.
PAGES = [
    "/",
    "/activity",
    "/agents",
    "/audit",
    "/connectors",
    "/connectors/adapters",
    "/connectors/directory",
    "/credentials",
    "/integrations",
    "/memory",
    "/settings",
    "/settings/otp",
    "/users",
    "/webhooks",
    "/workspaces",
]

# The naming convention for the attributes that stand in for a click handler.
# Plain data holders (data-scope, data-label, data-user) are not actions.
ACTION_ATTR = re.compile(
    r"data-("
    r"agent|workspace|credential|connector-type|binding|adapter|memory|proposal|"
    r"webhook|activity|directory|variant|actions|newbinding|download|copy|otp"
    r")-[a-z-]+"
)
SCRIPT_SRC = re.compile(r'<script src="(/static/js/[^"?]+)')


def _camel(attribute: str) -> str:
    """`data-agent-view` -> `agentView`, the name the dataset exposes."""
    parts = attribute.removeprefix("data-").split("-")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _available_script(page_html: str) -> str:
    """The page's inline script plus every static file it pulls in."""
    import pathlib

    sources = [page_html]
    for src in SCRIPT_SRC.findall(page_html):
        path = pathlib.Path("app/dashboard") / src.lstrip("/")
        if path.exists():
            sources.append(path.read_text())
    return "\n".join(sources)


@pytest.mark.parametrize("page", PAGES)
def test_every_action_attribute_has_a_listener_on_its_page(page, test_client, admin_token):
    r = test_client.get(page, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, page

    script = _available_script(r.text)
    unwired = set()
    # Candidates come from the scripts as well as the markup: the connector
    # directory builds its rows client-side, so its actions never appear in the
    # server response and an audit reading only that would pass no matter what
    # happened to the listeners.
    searchable = r.text + "\n" + script
    for match in {m.group(0) for m in ACTION_ATTR.finditer(searchable)}:
        dataset_name = _camel(match)
        wired = (
            dataset_name in script  # read via element.dataset
            or f"[{match}]" in script  # selected by attribute
            or f"[{match}=" in script  # selected by attribute value
            or f"getAttribute('{match}')" in script
            or f'getAttribute("{match}")' in script
        )
        if not wired:
            unwired.add(match)

    assert not unwired, f"{page} renders actions nothing listens for: {sorted(unwired)}"


def test_the_otp_button_is_wired_on_the_otp_page(test_client, admin_token):
    """The regression that prompted this file.

    The listener was added to the main settings script; the button is rendered
    by /settings/otp, which serves only its own. Clicking it did nothing.
    """
    r = test_client.get(
        "/settings/otp", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert r.status_code == 200
    assert "data-otp-confirm" in r.text, "the button moved or was renamed"
    assert "otpConfirm" in r.text, "nothing on this page reads the attribute"
