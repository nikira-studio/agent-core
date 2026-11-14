"""Activity > Tasks: a row stays a line or two; its record opens in the shared detail dialog."""

import pytest

from tests.browser.conftest import seed

SEED = '''
from app.database import get_db
from app.services import activity_service, agent_service, workspace_service
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
workspace_service.create_workspace("ops", "Operations", owner)
agent_service.create_agent(agent_id="it-admin", display_name="IT Admin", owner_user_id=owner,
                           read_scopes=["workspace:ops", "agent:it-admin"], write_scopes=["workspace:ops", "agent:it-admin"])
done = activity_service.create_activity(agent_id="it-admin", user_id=owner, task_description="Ship the importer", memory_scope="workspace:ops")
activity_service.set_handoff(done["id"], {"next_steps": ["Write the docs"], "open_questions": ["Which format?"]})
activity_service.update_activity(done["id"], status="completed", task_result="Importer shipped in commit abc123")
ask = activity_service.create_activity(agent_id="it-admin", user_id=owner, task_description="Approve the release notes", memory_scope="agent:it-admin")
activity_service.set_handoff(ask["id"], {"needs_human": "Please approve the release notes.", "needs_human_url": "https://studio.example.com/review/7"})
activity_service.update_activity(ask["id"], status="blocked", task_result="Waiting on a decision")
'''


@pytest.fixture(scope="module")
def tasks(instance, admin_page):
    seed(instance, SEED)
    admin_page.goto(instance + "/activity")
    admin_page.wait_for_load_state("load")
    return admin_page


def _row(page, text):
    return page.locator("tr.activity-row", has_text=text)


def test_a_row_stays_short_and_uses_plain_words(tasks):
    row = _row(tasks, "Ship the importer")
    assert row.locator("details").count() == 0  # nothing expands inside the cell
    assert 20 < row.bounding_box()["height"] < 110  # a line or two
    assert "Operations" in row.inner_text()
    private = _row(tasks, "Approve the release notes").inner_text()
    assert "private to IT Admin" in private and "agent:" not in private
    # a blocked row's actions: Done, Start and Cancel apply, so More holds two; a completed row has none of them
    assert _row(tasks, "Ship the importer").locator("[data-activity-done]").count() == 0


def test_the_title_and_the_detail_button_open_the_record_without_changing_the_row(tasks):
    row = _row(tasks, "Ship the importer")
    before = row.bounding_box()["height"]
    row.locator(".activity-title").click()
    tasks.wait_for_selector("#activity-detail-modal .modal", state="visible")
    modal = tasks.locator("#activity-detail-modal .modal")
    assert modal.locator(".modal-title").inner_text() == "Ship the importer"
    text = modal.inner_text()
    for present in ("Completed", "IT Admin", "Operations", "Started", "Last update",
                    "Result", "Importer shipped in commit abc123", "Next steps", "Write the docs",
                    "Open questions", "Which format?"):
        assert present.lower() in text.lower(), present
    assert "needs you" not in text.lower()  # a part with nothing in it is simply absent
    assert modal.locator(".modal-footer").inner_text().startswith("Close")
    tasks.keyboard.press("Escape")
    tasks.wait_for_selector("#activity-detail-modal .modal", state="hidden")
    assert abs(row.bounding_box()["height"] - before) < 1  # the row did not grow

    row.get_by_role("button", name="Detail").click()
    tasks.wait_for_selector("#activity-detail-modal .modal", state="visible")
    assert modal.locator(".modal-title").inner_text() == "Ship the importer"
    tasks.locator("#activity-detail-modal .modal-x").click()


def test_a_task_waiting_on_you_shows_its_ask_with_a_link_and_an_answer_box_and_its_actions(tasks):
    _row(tasks, "Approve the release notes").locator(".activity-title").click()
    tasks.wait_for_selector("#activity-detail-modal .modal", state="visible")
    modal = tasks.locator("#activity-detail-modal .modal")
    text = modal.inner_text()
    assert "needs you" in text.lower() and "Please approve the release notes." in text
    assert "the next time it works, and this closes" in text  # a private task is not "in private to IT Admin"
    assert modal.get_by_role("link", name="Open in studio.example.com").get_attribute("href") == "https://studio.example.com/review/7"
    assert modal.locator("#activity-answer-form input").is_visible()
    footer = modal.locator(".modal-footer")
    assert footer.get_by_role("button", name="Close").is_visible()
    assert footer.get_by_role("button", name="Done").is_visible() and footer.get_by_role("button", name="Cancel task").is_visible()
    tasks.keyboard.press("Escape")


def test_at_phone_width_the_dialog_is_a_sheet(instance, browser):
    from tests.browser.conftest import sign_in

    ctx = browser.new_context(viewport={"width": 390, "height": 700})
    page = ctx.new_page()
    try:
        sign_in(page, instance, "owner@example.com")
        page.goto(instance + "/activity")
        page.wait_for_load_state("load")
        page.locator(".activity-title", has_text="Ship the importer").first.click()
        page.wait_for_selector("#activity-detail-modal .modal", state="visible")
        width = page.evaluate("document.querySelector('#activity-detail-modal .modal').getBoundingClientRect().width")
        assert width == 390
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    finally:
        ctx.close()
