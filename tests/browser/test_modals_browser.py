"""The shared pop-up behaviour, in a real browser: opens at the top, X / Escape / outside click, focus returns,
only the body scrolls, an edit form does not discard silently, and a delete asks in the same kind of dialog."""

import pytest

from tests.browser.conftest import sign_in

OLD = "The weekly report goes out on Mondays at nine."
NEW = "The weekly report goes out on Fridays at noon. " + ("It is assembled from the finance and travel logs. " * 30)


def _api(page, path, body):
    return page.evaluate(
        "async ([p, b]) => { const r = await apiFetch(p, {method: 'POST', body: JSON.stringify(b)}); return r; }",
        [path, body],
    )


@pytest.fixture(scope="module")
def seeded(instance, admin_page):
    page = admin_page
    page.goto(instance + "/memory")
    page.wait_for_load_state("load")
    scope = page.evaluate("document.getElementById('current-scope').value")
    first = _api(page, "/api/memory/write", {"content": OLD, "memory_class": "fact", "scope": scope, "topic": "weekly report"})
    assert first["ok"], first
    second = _api(page, "/api/memory/write", {"content": NEW, "memory_class": "fact", "scope": scope, "topic": "weekly report",
                                              "supersedes_id": first["data"]["record"]["id"]})
    assert second["ok"], second
    return {"scope": scope, "new_id": second["data"]["record"]["id"], "old_id": first["data"]["record"]["id"]}


def _open_detail(page, base, record_id):
    page.goto(base + "/memory")
    page.wait_for_load_state("load")
    button = page.locator(f"[data-memory-detail='{record_id}']")
    button.focus()
    button.click()
    page.wait_for_selector("#memory-detail-modal .modal", state="visible")
    return button


def _geometry(page, overlay_id):
    return page.evaluate(
        """(id) => {
          const o = document.getElementById(id), m = o.querySelector('.modal'), s = o.querySelector('.modal-scroll');
          const h = m.querySelector('.modal-header').getBoundingClientRect();
          const f = m.querySelector('.modal-footer').getBoundingClientRect();
          const r = m.getBoundingClientRect();
          return {top: r.top, bottom: r.bottom, vh: innerHeight, headerTop: h.top, footerBottom: f.bottom,
                  scrollTop: s.scrollTop, scrollable: s.scrollHeight > s.clientHeight + 1,
                  pageScroll: window.scrollY, bodyLocked: getComputedStyle(document.body).overflow === 'hidden',
                  title: m.querySelector('.modal-title').textContent.trim(),
                  dialog: m.getAttribute('role'), labelled: !!m.getAttribute('aria-labelledby')};
        }""",
        overlay_id,
    )


def test_memory_detail_opens_at_the_top_with_a_real_title_and_the_content_once(instance, admin_page, seeded):
    page = admin_page
    _open_detail(page, instance, seeded["new_id"])
    g = _geometry(page, "memory-detail-modal")
    assert g["title"] == "weekly report"  # what the memory is about, not "Memory Detail"
    assert g["dialog"] == "dialog" and g["labelled"]
    assert g["scrollTop"] == 0 and g["headerTop"] >= 0 and g["top"] >= 0 and g["bottom"] <= g["vh"]
    assert g["footerBottom"] <= g["vh"] + 1 and g["bodyLocked"]
    text = page.locator("#memory-detail-modal .modal").inner_text()
    assert text.count("Fridays at noon") == 1  # the current content is shown once, not repeated in the history
    assert "Mondays at nine" in text or page.locator("#mem-history").is_visible()
    history = page.locator("#mem-history")
    assert history.is_visible() and "1 earlier version" in history.inner_text()
    assert history.get_attribute("open") is None  # collapsed until asked for
    # facts come before "how sure"; no separate Provenance block
    order = page.evaluate("() => [...document.querySelectorAll('#memory-detail-modal .mem-facts dt, #memory-detail-modal h3')].map(e => e.textContent.trim())")
    assert order[:2] == ["Kind", "Visible to"] and "Provenance" not in " ".join(order)


def test_only_the_body_scrolls(instance, admin_page, seeded):
    page = admin_page
    page.set_viewport_size({"width": 1300, "height": 420})  # short enough that the content has to scroll
    try:
        _open_detail(page, instance, seeded["new_id"])
        before = _geometry(page, "memory-detail-modal")
        assert before["scrollable"]
        page.mouse.move(650, 220)
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(150)
        after = _geometry(page, "memory-detail-modal")
        assert after["scrollTop"] > 0 and after["pageScroll"] == 0  # the dialog body moved, the page behind did not
        assert after["headerTop"] >= 0 and after["footerBottom"] <= after["vh"] + 1  # header and footer stayed put
        assert page.locator("#memory-detail-modal .modal-x").is_visible()
        # reopening starts at the top again
        page.locator("#memory-detail-modal .modal-x").click()
        page.locator("[data-memory-detail='%s']" % seeded["new_id"]).click()
        page.wait_for_selector("#memory-detail-modal .modal", state="visible")
        assert _geometry(page, "memory-detail-modal")["scrollTop"] == 0
    finally:
        page.set_viewport_size({"width": 1300, "height": 800})


def test_x_escape_and_outside_click_close_and_focus_returns_to_the_opener(instance, admin_page, seeded):
    page = admin_page
    opener = _open_detail(page, instance, seeded["new_id"])
    for how in ("x", "escape", "backdrop"):
        if how == "x":
            page.locator("#memory-detail-modal .modal-x").click()
        elif how == "escape":
            page.keyboard.press("Escape")
        else:
            page.mouse.click(8, 8)  # the dimmed area
        page.wait_for_selector("#memory-detail-modal .modal", state="hidden")
        assert page.evaluate("document.activeElement.getAttribute('data-memory-detail')") == seeded["new_id"], how
        assert page.evaluate("getComputedStyle(document.body).overflow") != "hidden"
        opener.click()
        page.wait_for_selector("#memory-detail-modal .modal", state="visible")
    page.keyboard.press("Escape")


def test_focus_stays_inside_the_dialog(instance, admin_page, seeded):
    page = admin_page
    _open_detail(page, instance, seeded["new_id"])
    for _ in range(12):
        page.keyboard.press("Tab")
        assert page.evaluate("!!document.activeElement.closest('#memory-detail-modal')")
    page.keyboard.press("Escape")


def test_an_edit_form_does_not_discard_typed_input_silently(instance, admin_page, seeded):
    page = admin_page
    page.goto(instance + "/memory")
    page.wait_for_load_state("load")
    opener = page.get_by_role("button", name="Save Memory").first
    opener.focus()
    opener.click()
    page.wait_for_selector("#write-memory-modal .modal", state="visible")
    g = _geometry(page, "write-memory-modal")
    assert g["title"] == "Save Memory" and g["scrollTop"] == 0
    assert page.evaluate("document.activeElement.id") == "mem-content"  # focus goes to the first field

    # nothing typed yet: an outside click just closes
    page.mouse.click(8, 8)
    page.wait_for_selector("#write-memory-modal .modal", state="hidden")
    opener.click()
    page.wait_for_selector("#write-memory-modal .modal", state="visible")

    page.fill("#mem-content", "Something half written")
    page.mouse.click(8, 8)  # outside click with unsaved input: asks first
    page.wait_for_selector("#confirm-dialog .modal", state="visible")
    assert page.locator("#confirm-dialog h2").inner_text() == "Discard what you typed?"
    page.get_by_role("button", name="Keep editing").click()
    page.wait_for_selector("#confirm-dialog", state="detached")
    assert page.locator("#write-memory-modal .modal").is_visible()
    assert page.input_value("#mem-content") == "Something half written"

    page.keyboard.press("Escape")  # Escape asks the same question
    page.wait_for_selector("#confirm-dialog .modal", state="visible")
    page.get_by_role("button", name="Discard").click()
    page.wait_for_selector("#write-memory-modal .modal", state="hidden")
    assert "Save Memory" in page.evaluate("document.activeElement.textContent")  # focus came back to the opener


def test_a_delete_asks_in_the_same_dialog_and_names_what_it_deletes(instance, admin_page, seeded):
    page = admin_page
    page.goto(instance + "/memory")
    page.wait_for_load_state("load")
    row_button = page.locator(f"[data-memory-retract='{seeded['new_id']}'], [onclick*=\"retractRecord('{seeded['new_id']}')\"]").first
    row_button.focus()
    row_button.click()
    page.wait_for_selector("#confirm-dialog .modal", state="visible")
    dialog = page.locator("#confirm-dialog .modal")
    assert dialog.get_attribute("role") == "dialog" and dialog.get_attribute("aria-modal") == "true"
    title = page.locator("#confirm-dialog h2").inner_text()
    assert title.startswith("Retract this memory") and "Fridays at noon" in title  # names which memory
    assert page.locator("#confirm-dialog").get_by_role("button", name="Retract").is_visible()
    assert page.evaluate("document.activeElement.textContent") == "Cancel"  # the safe choice has focus
    for _ in range(4):  # Tab cycles inside the two buttons
        page.keyboard.press("Tab")
        assert page.evaluate("!!document.activeElement.closest('#confirm-dialog')")
    page.keyboard.press("Escape")
    page.wait_for_selector("#confirm-dialog", state="detached")
    # nothing happened: still active
    status = page.evaluate("async (id) => (await apiFetch('/api/memory/' + id)).data.record.record_status", seeded["new_id"])
    assert status == "active"
    assert page.evaluate("document.activeElement.getAttribute('data-memory-retract')") == seeded["new_id"]  # focus came back


def test_at_phone_width_the_dialog_is_a_sheet_with_header_and_footer_in_view(instance, browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 700})
    page = ctx.new_page()
    try:
        sign_in(page, instance, "owner@example.com")
        page.goto(instance + "/agents")
        page.wait_for_load_state("load")
        page.evaluate("openModal('create-agent-modal')")
        page.wait_for_selector("#create-agent-modal .modal", state="visible")
        g = _geometry(page, "create-agent-modal")
        assert g["scrollable"] and g["scrollTop"] == 0
        width = page.evaluate("document.querySelector('#create-agent-modal .modal').getBoundingClientRect().width")
        assert width == 390  # full-width sheet
        page.evaluate("document.querySelector('#create-agent-modal .modal-scroll').scrollTop = 99999")
        g2 = _geometry(page, "create-agent-modal")
        assert g2["scrollTop"] > 0 and g2["headerTop"] >= 0 and g2["footerBottom"] <= g2["vh"] + 1
        assert page.locator("#create-agent-modal .modal-x").is_visible()
    finally:
        ctx.close()
