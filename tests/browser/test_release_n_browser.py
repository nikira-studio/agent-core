"""Release N: the phone checks for the pages that were hard to read on a narrow screen.

- The approval detail page: every table fits its card at 390 px.
- The Users page: the values in a row all start at the same place, so a value that wraps lines up
  under the value and not under the label.

The browser tests cannot run in every sandbox; when Playwright is missing these are skipped.
"""

from pathlib import Path

import pytest

from tests.browser.conftest import seed, sign_in

SEED = '''
import os
from app.database import get_db
from app.services import approval_service, agent_service, workspace_service
from app.services.auth_service import create_user

with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]

# A few people, so the Users table has more than one row to line up.
create_user("rel-user-one", "rel-one@example.test", "not-a-real-password", "Release One", "user")
create_user("rel-user-two", "rel-two@example.test", "not-a-real-password", "Release Two", "user")

workspace_service.create_workspace("rel-ws", "Release Place", owner)
agent_service.create_agent(agent_id="rel-agent", display_name="Release Agent", owner_user_id=owner,
                           read_scopes=["workspace:rel-ws"], write_scopes=["workspace:rel-ws"],
                           capabilities=["memory", "admin_propose"])
agent_service.create_agent(agent_id="rel-target", display_name="Release Target", owner_user_id=owner,
                           read_scopes=["workspace:rel-ws"], write_scopes=[], capabilities=["memory"])

# A proposal that waits for a decision: its detail page has four tables.
approval = approval_service.propose(
    agent_id="rel-agent", scope="workspace:rel-ws", title="Widen the phone layout",
    operations=[{"name": "agent.update", "params": {
        "agent_id": "rel-target", "display_name": "Release Target",
        "description": "A description long enough that it has to wrap on a narrow phone screen",
        "read_scopes": ["workspace:rel-ws"], "write_scopes": [], "capabilities": ["memory"],
    }}],
)
with open(os.path.join(os.environ["AGENT_CORE_DATA_PATH"], "release-n-approval.txt"), "w") as fh:
    fh.write(approval["id"])
'''

PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def phone(instance, browser):
    context = browser.new_context(viewport=PHONE)
    page = context.new_page()
    sign_in(page, instance, "owner@example.com")  # the first sign-in creates the owner the seed needs
    seed(instance, SEED)
    yield page
    context.close()


def _approval_id(instance) -> str:
    return (Path(instance.data) / "release-n-approval.txt").read_text().strip()


def test_the_approval_detail_page_fits_a_phone(phone, instance):
    """Every table on the detail page stays inside its card: no value is cut off at the right edge."""
    phone.goto(instance + "/approvals/" + _approval_id(instance))
    phone.wait_for_load_state("load")
    phone.wait_for_selector(".card table")
    assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    outside = phone.evaluate(
        """
        () => {
          const bad = [];
          document.querySelectorAll('.card table').forEach(function (table) {
            const card = table.closest('.card');
            if (!card) return;
            const t = table.getBoundingClientRect();
            const c = card.getBoundingClientRect();
            if (t.right > c.right + 1) {
              bad.push(Math.round(t.right) + ' past the card at ' + Math.round(c.right));
            }
          });
          return bad;
        }
        """
    )
    assert not outside, outside


def test_the_users_page_lines_the_values_up(phone, instance):
    """In a stacked row every value starts at the same x, so a value that wraps lines up under itself."""
    phone.goto(instance + "/users")
    phone.wait_for_load_state("load")
    phone.wait_for_selector("table.stack-phone tbody tr")
    lefts = phone.evaluate(
        """
        () => {
          const row = document.querySelector('table.stack-phone tbody tr');
          if (!row) return [];
          const out = [];
          row.querySelectorAll('td').forEach(function (cell) {
            const range = document.createRange();
            range.selectNodeContents(cell);
            const rect = range.getBoundingClientRect();
            if (rect.width || rect.height) out.push(rect.left);
          });
          return out;
        }
        """
    )
    assert len(lefts) >= 3, lefts
    assert max(lefts) - min(lefts) <= 2, lefts
