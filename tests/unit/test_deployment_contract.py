"""The shipped image and the documented contract have to agree.

Both of these were wrong at the same time and neither was visible from the
test suite: the image defaulted to four workers while several pieces of state
are per-process, and CI tested a Python version the image does not ship. Config
drift like this produces no failure anywhere — it just quietly makes the tests
a statement about a different program than the one users run.
"""

import pathlib
import re

DOCKERFILE = pathlib.Path("Dockerfile").read_text()
CI = pathlib.Path(".github/workflows/ci.yml").read_text()


def _image_python_version() -> str:
    match = re.search(r"FROM[^\n]*python:(\d+\.\d+)", DOCKERFILE)
    assert match, "could not find the Python base image in the Dockerfile"
    return match.group(1)


def test_the_image_defaults_to_a_single_worker():
    """Rate limits, the search guard and the event stream are per process."""
    match = re.search(r"AGENT_CORE_WORKERS:-(\d+)", DOCKERFILE)
    assert match, "the worker count is no longer set with a default"
    assert match.group(1) == "1", (
        "a second worker multiplies every configured limit and splits the "
        "dashboard event stream; raising this is an operator's decision, not a default"
    )


def test_ci_tests_the_version_the_image_ships():
    version = _image_python_version()
    assert f'"{version}"' in CI, (
        f"the image ships Python {version}; CI must run the suite on it"
    )


def test_the_worker_contract_is_documented():
    """An operator raising this needs to know what it costs."""
    configuration = pathlib.Path("docs/configuration.md").read_text()
    assert "AGENT_CORE_WORKERS" in configuration
    assert "single process" in configuration
