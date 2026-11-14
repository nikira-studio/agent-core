"""The one row of Settings tabs, clicked in a real browser: every tab from every page it can be reached from.

Skipped when Playwright or its browser is not installed. Uses the shared throwaway instance (conftest.py).
"""

import re

import pytest

from tests.browser.conftest import PASSWORD, sign_in

ADMIN_TABS = ["Account", "Memory", "Data and backup", "Security", "Users", "Webhooks"]
PANEL_OF = {"Account": "account", "Memory": "memory", "Data and backup": "data", "Security": "security"}
PAGE_OF = {"Users": "/users", "Webhooks": "/webhooks"}


def _row(page):
    return page.locator("nav.page-tabs[aria-label=Settings] a")


def _visible_panels(page):
    return page.evaluate("() => [...document.querySelectorAll('[data-tab-panel]')].filter(p => !p.hidden).map(p => p.dataset.tabPanel)")


def _assert_tab_state(page, base, label):
    """After clicking `label`: the URL is right, the heading is Settings, exactly that tab is current, and the panel shows."""
    if label in PAGE_OF:
        page.wait_for_url(base + PAGE_OF[label], timeout=10000)
        assert page.locator("main h1").all_inner_texts() == ["Settings"]
        assert _visible_panels(page) == []  # these are other pages: no settings panels on them
    else:
        key = PANEL_OF[label]
        page.wait_for_function("k => location.pathname === '/settings' && location.hash === '#' + k", arg=key, timeout=10000)
        assert page.locator("main h1").all_inner_texts() == ["Settings"]
        assert _visible_panels(page) == [key]
    current = page.locator("nav.page-tabs[aria-label=Settings] a[aria-current=page]").all_inner_texts()
    assert current == [label]
    assert _row(page).all_inner_texts() == ADMIN_TABS  # still one row, same six


@pytest.mark.parametrize("start", ["/settings", "/users", "/webhooks"])
def test_all_six_tabs_work_from_each_page(instance, admin_page, start):
    page = admin_page
    for label in ADMIN_TABS:
        page.goto(instance + start)
        page.wait_for_load_state("load")
        assert _row(page).all_inner_texts() == ADMIN_TABS
        _row(page).filter(has_text=re.compile(rf"^{re.escape(label)}$")).click()
        _assert_tab_state(page, instance, label)


@pytest.mark.parametrize("key", ["account", "memory", "data", "security"])
def test_a_first_load_with_a_hash_shows_that_panel(instance, admin_page, key):
    for start in ("/settings", "/users", "/webhooks"):
        admin_page.goto(instance + "/settings#" + key)
        admin_page.wait_for_load_state("load")
        assert _visible_panels(admin_page) == [key]
        assert admin_page.locator("nav.page-tabs a[aria-current=page]").all_inner_texts() == [
            next(k for k, v in PANEL_OF.items() if v == key)
        ]
    admin_page.goto(instance + "/settings#nonsense")
    admin_page.wait_for_load_state("load")
    assert _visible_panels(admin_page) == ["account"]  # an unknown hash falls back to the first panel


def test_the_back_button_follows_tab_changes(instance, admin_page):
    admin_page.goto(instance + "/users")
    admin_page.wait_for_load_state("load")
    _row(admin_page).filter(has_text=re.compile(r"^Security$")).click()
    admin_page.wait_for_url(instance + "/settings#security")
    admin_page.go_back()
    admin_page.wait_for_url(instance + "/users")
    assert admin_page.locator("main h1").all_inner_texts() == ["Settings"]


def test_a_non_admin_sees_settings_with_no_tab_row(instance, browser, admin_page):
    status = admin_page.evaluate(
        """async (pw) => { const r = await apiFetch('/api/auth/users', {method: 'POST', body: JSON.stringify(
            {display_name: 'Second', email: 'second@example.com', password: pw, role: 'user'})}); return r.ok; }""",
        PASSWORD,
    )
    assert status
    ctx = browser.new_context(viewport={"width": 1300, "height": 800})
    page = ctx.new_page()
    try:
        sign_in(page, instance, "second@example.com")
        page.goto(instance + "/settings")
        page.wait_for_load_state("load")
        assert page.locator("main h1").all_inner_texts() == ["Settings"]
        assert _row(page).count() == 0  # one tab would be a menu of one
        assert _visible_panels(page) == ["account"]
        for path in ("/users", "/webhooks"):
            resp = page.goto(instance + path)
            assert resp.status == 403
    finally:
        ctx.close()


def test_a_link_to_a_field_opens_its_tab_and_lands_on_the_field(instance, admin_page):
    page = admin_page
    page.goto(instance + "/settings#retracted-retention-days")
    page.wait_for_load_state("load")
    assert _visible_panels(page) == ["data"]
    assert page.evaluate("document.activeElement.id") == "retracted-retention-days"
    in_view = page.evaluate("(() => { const r = document.getElementById('retracted-retention-days').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; })()")
    assert in_view
    assert page.locator("nav.page-tabs a[aria-current=page]").all_inner_texts() == ["Data and backup"]


def test_a_fold_is_a_quiet_text_toggle_not_a_full_width_box(instance, admin_page):
    page = admin_page
    page.goto(instance + "/settings#memory")
    page.wait_for_load_state("load")
    summary = page.locator("#tab-memory details > summary").first
    summary.click()  # a mouse click leaves no focus box
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") in ("none", "")
    widths = page.evaluate("(() => { const s = document.querySelector('#tab-memory details > summary'); return [s.getBoundingClientRect().width, s.closest('.card').getBoundingClientRect().width]; })()")
    assert widths[0] < widths[1] * 0.6  # it hugs its words
    assert page.evaluate("getComputedStyle(document.querySelector('#tab-memory details > summary'), '::before').content").startswith('"')
