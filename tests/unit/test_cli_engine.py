"""Unit tests for the declarative CLI engine."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from app.connectors.base import Credential
from app.connectors.cli_engine import CliEngine


def make_ct(backend_json: dict) -> dict:
    return {"id": "test-cli", "backend_json": json.dumps(backend_json)}


def make_cred(raw: str = "", fields: dict | None = None) -> Credential:
    return Credential(raw=raw, fields=fields or {}, reference_name="test-ref")


# ─── Fixtures ────────────────────────────────────────────────────────────────


class TestCliEngineInit:
    def test_parses_bin(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        assert engine.bin == "gh"

    def test_parses_default_timeout(self):
        engine = CliEngine(make_ct({"bin": "gh", "timeout": 60, "commands": {}}))
        assert engine.default_timeout == 60

    def test_parses_env_spec(self):
        engine = CliEngine(
            make_ct(
                {"bin": "gh", "env": {"GH_TOKEN": "{{ cred.token }}"}, "commands": {}}
            )
        )
        assert engine.env_spec == {"GH_TOKEN": "{{ cred.token }}"}

    def test_parses_commands(self):
        commands = {"list_repos": {"args": ["repo", "list"]}}
        engine = CliEngine(make_ct({"bin": "gh", "commands": commands}))
        assert engine.commands == commands


# ─── Templating ─────────────────────────────────────────────────────────────


class TestCliEngineTemplating:
    def test_render_params_simple(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._render("hello {{ params.name }}", {"name": "world"}, {})
        assert result == "hello world"

    def test_render_params_missing(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._render("hello {{ params.name }}", {}, {})
        assert result == "hello {{ params.name }}"

    def test_render_params_default_str(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._render("hello {{ params.name | default('') }}", {}, {})
        assert result == "hello "

    def test_render_params_default_int(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._render("count={{ params.count | default(0, as=int) }}", {}, {})
        assert result == "count=0"

    def test_render_params_default_omit(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._render("{{ params.owner | default('', as=omit) }}", {}, {})
        assert result == "__OMIT__"

    def test_render_cred_raw(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        cred = make_cred(raw="secret-token")
        result = engine._render(
            "token={{ cred.raw }}",
            {},
            {},
            cred,
        )
        assert result == "token=secret-token"

    def test_render_config(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._render("dir={{ config.workdir }}", {}, {"workdir": "/tmp"})
        assert result == "dir=/tmp"

    def test_render_multiple_placeholders(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._render(
            "{{ params.a }} + {{ params.b }}",
            {"a": "1", "b": "2"},
            {},
        )
        assert result == "1 + 2"


# ─── __OMIT__ Sentinel ──────────────────────────────────────────────────────


class TestOmitSentinel:
    def test_omit_sentinel_drops_flag_value_pair(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        argv = ["--owner", "__OMIT__", "--limit", "30"]
        result = engine._apply_omit_sentinel(argv)
        assert result == ["--limit", "30"]

    def test_omit_sentinel_leaves_non_flag_elements(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        argv = ["repo", "list", "__OMIT__", "--limit", "30"]
        result = engine._apply_omit_sentinel(argv)
        assert result == ["repo", "list", "--limit", "30"]

    def test_omit_sentinel_preserves_standalone_value(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        argv = ["--owner", "myorg", "__OMIT__", "--limit", "30"]
        result = engine._apply_omit_sentinel(argv)
        assert result == ["--owner", "myorg", "--limit", "30"]

    def test_omit_sentinel_multiple_pairs(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        argv = ["--owner", "__OMIT__", "--limit", "__OMIT__"]
        result = engine._apply_omit_sentinel(argv)
        assert result == []

    def test_omit_sentinel_drops_flag_when_value_is_empty(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        argv = engine._build_argv(
            {"args": ["--owner", "{{ params.owner | default('', as=omit) }}"]},
            {},
            {},
            None,
        )
        assert "--owner" not in argv
        assert argv == []


# ─── Argv Building ─────────────────────────────────────────────────────────


class TestArgvBuilding:
    def test_build_argv_simple(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        argv = engine._build_argv(
            {"args": ["repo", "list", "--limit", "30"]},
            {},
            {},
            None,
        )
        assert argv == ["repo", "list", "--limit", "30"]

    def test_build_argv_with_params(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        argv = engine._build_argv(
            {"args": ["--owner", "{{ params.owner }}"]},
            {"owner": "myorg"},
            {},
            None,
        )
        assert argv == ["--owner", "myorg"]

    def test_build_argv_with_owner_present_keeps_flag(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        argv = engine._build_argv(
            {"args": ["--owner", "{{ params.owner | default('', as=str) }}"]},
            {"owner": "myorg"},
            {},
            None,
        )
        assert argv == ["--owner", "myorg"]


# ─── Env Building ───────────────────────────────────────────────────────────


class TestEnvBuilding:
    def test_build_env_renders_template(self):
        engine = CliEngine(
            make_ct(
                {"bin": "gh", "env": {"GH_TOKEN": "{{ cred.token }}"}, "commands": {}}
            )
        )
        cred = make_cred(raw="my-secret-token", fields={"token": "my-secret-token"})
        env = engine._build_env({}, {}, cred)
        assert env == {"GH_TOKEN": "my-secret-token"}

    def test_build_env_empty_for_missing_cred(self):
        engine = CliEngine(
            make_ct(
                {"bin": "gh", "env": {"GH_TOKEN": "{{ cred.token }}"}, "commands": {}}
            )
        )
        env = engine._build_env({}, {}, None)
        assert env == {"GH_TOKEN": "{{ cred.token }}"}  # falls back to raw template


# ─── Output Parsing ────────────────────────────────────────────────────────


class TestOutputParsing:
    def test_parse_text(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._parse_output({"parse": {"type": "text"}}, "hello world")
        assert result == {"success": True, "output": "hello world"}

    def test_parse_jsonpath(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._parse_output(
            {"parse": {"type": "jsonpath", "path": "$.name"}},
            '{"name": "alice", "age": 30}',
        )
        assert result == {"success": True, "output": "alice"}

    def test_parse_jsonpath_full_output(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._parse_output(
            {"parse": {"type": "jsonpath", "path": "$"}},
            '[{"name": "repo1"}, {"name": "repo2"}]',
        )
        assert result["success"] is True
        assert len(result["output"]) == 2

    def test_parse_jsonpath_invalid_json(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._parse_output(
            {"parse": {"type": "jsonpath", "path": "$"}},
            "not json",
        )
        assert result["success"] is False
        assert "Failed to parse JSON" in result["error"]

    def test_parse_regex_match(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._parse_output(
            {"parse": {"type": "regex", "path": r"clone_url:\s*(\S+)"}},
            "clone_url: https://github.com/owner/repo.git",
        )
        assert result["success"] is True
        assert result["output"] == "https://github.com/owner/repo.git"

    def test_parse_regex_no_match(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine._parse_output(
            {"parse": {"type": "regex", "path": r"notfound:(\S+)"}},
            "some other text",
        )
        assert result["success"] is False
        assert "not found" in result["error"]


# ─── Execute ─────────────────────────────────────────────────────────────────


class TestCliEngineExecute:
    @patch("app.connectors.cli_engine.subprocess.run")
    def test_execute_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b'{"name": "alice"}',
            stderr=b"",
        )
        engine = CliEngine(
            make_ct(
                {
                    "bin": "gh",
                    "commands": {
                        "get_user": {
                            "args": ["api", "user"],
                            "parse": {"type": "jsonpath", "path": "$.name"},
                        }
                    },
                }
            )
        )
        result = engine.execute("get_user", {}, make_cred(raw="tok"), "{}", None)
        assert result["success"] is True
        assert result["output"] == "alice"

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["gh", "api", "user"]
        assert call_args[1]["shell"] is False
        assert call_args[1]["capture_output"] is True

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_execute_with_params(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]", stderr=b"")
        engine = CliEngine(
            make_ct(
                {
                    "bin": "gh",
                    "commands": {
                        "list_repos": {
                            "args": [
                                "repo",
                                "list",
                                "--owner",
                                "{{ params.owner | default('', as=str) }}",
                                "--limit",
                                "{{ params.limit | default(30, as=int) }}",
                            ],
                            "parse": {"type": "jsonpath", "path": "$"},
                        }
                    },
                }
            )
        )
        result = engine.execute(
            "list_repos",
            {"owner": "myorg", "limit": 10},
            make_cred(raw="tok"),
            "{}",
            None,
        )
        assert result["success"] is True

        argv = mock_run.call_args[0][0]
        assert "--owner" in argv
        assert "myorg" in argv
        assert "--limit" in argv
        assert "10" in argv
        # owner was provided so not omitted
        owner_idx = argv.index("--owner")
        assert argv[owner_idx + 1] == "myorg"

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_execute_omit_flag_when_owner_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]", stderr=b"")
        engine = CliEngine(
            make_ct(
                {
                    "bin": "gh",
                    "commands": {
                        "list_repos": {
                            "args": [
                                "repo",
                                "list",
                                "--json",
                                "name",
                                "--owner",
                                "{{ params.owner | default('', as=omit) }}",
                                "--limit",
                                "30",
                            ],
                            "parse": {"type": "jsonpath", "path": "$"},
                        }
                    },
                }
            )
        )
        result = engine.execute("list_repos", {}, make_cred(raw="tok"), "{}", None)
        assert result["success"] is True

        argv = mock_run.call_args[0][0]
        # --owner and its value should be dropped
        assert "--owner" not in argv
        assert "myorg" not in argv

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_execute_nonzero_exit_returns_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"not found")
        engine = CliEngine(
            make_ct(
                {
                    "bin": "gh",
                    "commands": {
                        "get_user": {
                            "args": ["api", "user"],
                            "parse": {"type": "text"},
                        }
                    },
                }
            )
        )
        result = engine.execute("get_user", {}, make_cred(raw="tok"), "{}", None)
        assert result["success"] is False
        assert result["error_code"] == "EXEC_ERROR"
        assert "not found" in result["error"]

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_execute_custom_success_exit_codes(self, mock_run):
        mock_run.return_value = MagicMock(returncode=3, stdout=b"warn", stderr=b"")
        engine = CliEngine(
            make_ct(
                {
                    "bin": "gh",
                    "commands": {
                        "get_user": {
                            "args": ["api", "user"],
                            "success_exit_codes": [0, 3],
                            "parse": {"type": "text"},
                        }
                    },
                }
            )
        )
        result = engine.execute("get_user", {}, make_cred(raw="tok"), "{}", None)
        assert result["success"] is True

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_execute_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("gh", 30)
        engine = CliEngine(
            make_ct(
                {
                    "bin": "gh",
                    "timeout": 30,
                    "commands": {
                        "get_user": {
                            "args": ["api", "user"],
                            "parse": {"type": "text"},
                        }
                    },
                }
            )
        )
        result = engine.execute("get_user", {}, make_cred(raw="tok"), "{}", None)
        assert result["success"] is False
        assert result["error_code"] == "TIMEOUT"
        assert "timed out" in result["error"]

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_execute_stdin(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"ok", stderr=b"")
        engine = CliEngine(
            make_ct(
                {
                    "bin": "kubectl",
                    "commands": {
                        "apply": {
                            "args": ["apply", "-f", "-"],
                            "stdin": "{{ params.manifest }}",
                            "parse": {"type": "text"},
                        }
                    },
                }
            )
        )
        result = engine.execute(
            "apply",
            {"manifest": "apiVersion: v1\nkind: Pod"},
            make_cred(),
            "{}",
            None,
        )
        assert result["success"] is True
        call_args = mock_run.call_args
        assert call_args[1]["input"] == b"apiVersion: v1\nkind: Pod"
        assert call_args[1]["stdin"] == subprocess.PIPE

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_execute_env_passed_explicitly(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"{}", stderr=b"")
        engine = CliEngine(
            make_ct(
                {
                    "bin": "gh",
                    "env": {"GH_TOKEN": "{{ cred.raw }}"},
                    "commands": {
                        "whoami": {
                            "args": ["api", "user"],
                            "parse": {"type": "jsonpath", "path": "$"},
                        }
                    },
                }
            )
        )
        engine.execute(
            "whoami",
            {},
            make_cred(raw="my-token", fields={"token": "my-token"}),
            "{}",
            None,
        )
        env = mock_run.call_args[1]["env"]
        assert env == {"GH_TOKEN": "my-token"}

    def test_execute_unknown_action_returns_error(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine.execute("unknown", {}, make_cred(), "{}", None)
        assert result["success"] is False
        assert "No command defined" in result["error"]


# ─── refresh_session ────────────────────────────────────────────────────────


class TestRefreshSession:
    def test_refresh_session_returns_error(self):
        engine = CliEngine(make_ct({"bin": "gh", "commands": {}}))
        result = engine.refresh_session(make_cred(), "{}", None)
        assert "error" in result
        assert "cli does not support session refresh" in result["error"]
