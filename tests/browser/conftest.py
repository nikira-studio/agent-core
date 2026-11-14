"""Shared fixtures for the real-browser tests: a throwaway instance, a Chromium, a signed-in admin page."""

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

REPO = Path(__file__).resolve().parents[2]


class Instance(str):
    """The base URL of a throwaway instance; ``.data`` is its data folder."""

    data: str = ""

PASSWORD = "-".join(["browser", "test", "owner", "9731"])  # a throwaway instance; built so it is plainly not a real secret


@pytest.fixture(scope="module")
def instance():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data = tempfile.mkdtemp(prefix="ac-browser-")
    env = dict(os.environ, AGENT_CORE_DATA_PATH=data, AGENT_CORE_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = Instance(f"http://127.0.0.1:{port}")
    base.data = data
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("throwaway instance did not start")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser installed
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield b
        b.close()


def sign_in(page, base, email, name=None):
    page.goto(base + "/")
    page.wait_for_load_state("load")
    if page.query_selector("input[name=display_name]"):  # first run: create the owner
        page.fill("input[name=email]", email)
        page.fill("input[name=display_name]", name or "Owner")
        page.fill("input[name=password]", PASSWORD)
    else:
        page.fill("input[name=email]", email)
        page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r".*/($|\?.*|#.*)"), timeout=15000)


@pytest.fixture(scope="module")
def admin_page(instance, browser):
    ctx = browser.new_context(viewport={"width": 1300, "height": 800})
    page = ctx.new_page()
    sign_in(page, instance, "owner@example.com")
    yield page
    ctx.close()




def seed(base: str, code: str) -> None:
    """Run a snippet of Python against the throwaway instance's own database (its data folder), for test data that has
    no dashboard route (an agent's task, say)."""
    env = dict(os.environ, AGENT_CORE_DATA_PATH=base.data)
    subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, check=True, capture_output=True)
