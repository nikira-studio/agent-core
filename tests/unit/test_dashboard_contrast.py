"""The colours the dashboard puts text on must stay readable (WCAG AA, 4.5:1) in both themes."""

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[2] / "app/dashboard/static/css/dashboard.css").read_text()


def _tokens(block_start: str) -> dict[str, str]:
    start = CSS.index(block_start)
    body = CSS[CSS.index("{", start) + 1 : CSS.index("}", start)]
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", body))


def _luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _themes():
    dark = _tokens(":root {")
    light = {**dark, **_tokens('[data-theme="light"]')}
    return {"dark": dark, "light": light}


def test_white_text_on_filled_controls_reads_in_both_themes():
    for name, t in _themes().items():
        for fill in ("accent-fill", "accent-fill-hover", "danger-fill", "danger-fill-hover"):
            assert _ratio("#ffffff", t[fill]) >= 4.5, (name, fill)


def test_muted_text_reads_on_every_panel_in_both_themes():
    for name, t in _themes().items():
        for panel in ("bg", "panel", "panel2"):
            assert _ratio(t["muted"], t[panel]) >= 4.5, (name, panel)


def test_the_accent_and_warning_text_read_on_the_page_in_both_themes():
    for name, t in _themes().items():
        for panel in ("bg", "panel"):
            assert _ratio(t["accent"], t[panel]) >= 4.5, (name, "accent", panel)
            assert _ratio(t["danger"], t[panel]) >= 4.5, (name, "danger", panel)
        assert _ratio("#1a1a1f", t["warning"]) >= 4.5, (name, "text on warning fill")
    light = _themes()["light"]
    assert _ratio(light["warning-text"], "#fef6ea") >= 4.5
