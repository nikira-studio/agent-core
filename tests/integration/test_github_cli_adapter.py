"""Integration tests for the GitHub CLI adapter manifest."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.connectors.base import Credential
from app.connectors.cli_engine import CliEngine
from app.connectors.manifest import load_and_validate


def make_gh_cred(token: str = "ghp_testtoken") -> Credential:
    return Credential(raw=token, fields={"token": token}, reference_name="gh-ref")


class TestGitHubCliAdapterManifest:
    def test_github_cli_manifest_loads_valid(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None, f"Expected no error, got: {err}"
        assert m is not None
        assert m.id == "github_cli"
        assert m.spec_version == "1.0"
        assert m.version == "1.0.0"
        assert m.backend["type"] == "cli"
        assert m.backend["bin"] == "gh"

    def test_github_cli_actions_present(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None
        action_names = [a["name"] for a in m.actions]
        for action in ["list_repos", "list_issues", "create_issue"]:
            assert action in action_names, f"Missing action: {action}"

    def test_github_cli_requires_gh_binary(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None
        assert m.requires is not None
        assert "gh" in m.requires.get("bins", [])

    def test_github_cli_credential_schema(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None
        fields = m.credential_schema["fields"]
        assert len(fields) == 1
        assert fields[0]["name"] == "token"
        assert fields[0]["secret"] is True
        assert fields[0]["required"] is True

    def test_github_cli_all_commands_use_json_output(self):
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None
        commands = m.backend.get("commands", {})
        for action_name, cmd_def in commands.items():
            parse_type = cmd_def.get("parse", {}).get("type")
            assert parse_type == "jsonpath", (
                f"Action {action_name} should use jsonpath parse type"
            )


class TestGitHubCliAdapterWireLevel:
    @patch("app.connectors.cli_engine.subprocess.run")
    def test_list_repos_builds_correct_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [{"name": "repo1", "url": "https://github.com/owner/repo1"}]
            ).encode(),
            stderr=b"",
        )
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})

        result = engine.execute(
            "list_repos",
            {"owner": "myorg", "limit": 10},
            make_gh_cred(),
            "{}",
            None,
        )

        assert result["success"] is True
        argv = mock_run.call_args[0][0]
        assert argv[0] == "gh"
        assert "repo" in argv
        assert "list" in argv
        assert "--json" in argv
        assert "--owner" in argv
        assert "myorg" in argv
        assert "--limit" in argv
        assert "10" in argv

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_list_repos_omits_owner_when_not_provided(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([]).encode(),
            stderr=b"",
        )
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})

        result = engine.execute("list_repos", {}, make_gh_cred(), "{}", None)

        assert result["success"] is True
        argv = mock_run.call_args[0][0]
        # --owner flag should be dropped when not provided
        assert "--owner" not in argv

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_list_repos_passes_token_via_env(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]", stderr=b"")
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})

        engine.execute("list_repos", {}, make_gh_cred("ghp_mysecret"), "{}", None)

        env = mock_run.call_args[1]["env"]
        assert env["GH_TOKEN"] == "ghp_mysecret"
        assert env["GITHUB_TOKEN"] == "ghp_mysecret"

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_list_issues_requires_repo_param(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([]).encode(),
            stderr=b"",
        )
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})

        result = engine.execute(
            "list_issues",
            {"repo": "owner/repo"},
            make_gh_cred(),
            "{}",
            None,
        )

        assert result["success"] is True
        argv = mock_run.call_args[0][0]
        assert "issue" in argv
        assert "list" in argv
        assert "--repo" in argv
        assert "owner/repo" in argv

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_create_issue_builds_correct_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"url": "https://github.com/owner/repo/issues/5", "number": 5}
            ).encode(),
            stderr=b"",
        )
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})

        result = engine.execute(
            "create_issue",
            {
                "repo": "owner/repo",
                "title": "Bug: login fails",
                "body": "Steps to reproduce...",
            },
            make_gh_cred(),
            "{}",
            None,
        )

        assert result["success"] is True
        argv = mock_run.call_args[0][0]
        assert "issue" in argv
        assert "create" in argv
        assert "--json" in argv
        assert "url,number" in argv
        assert "--title" in argv
        assert "Bug: login fails" in argv
        assert "--repo" in argv
        assert "owner/repo" in argv

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_create_issue_with_optional_body(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"url": "https://github.com/owner/repo/issues/6", "number": 6}
            ).encode(),
            stderr=b"",
        )
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})

        result = engine.execute(
            "create_issue",
            {"repo": "owner/repo", "title": "Simple issue"},
            make_gh_cred(),
            "{}",
            None,
        )

        assert result["success"] is True

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_shell_false_is_always_set(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]", stderr=b"")
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})
        engine.execute("list_repos", {}, make_gh_cred(), "{}", None)

        assert mock_run.call_args[1]["shell"] is False

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_env_passed_explicitly_no_inherit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]", stderr=b"")
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})
        engine.execute("list_repos", {}, make_gh_cred(), "{}", None)

        env = mock_run.call_args[1]["env"]
        # env should be a dict passed explicitly, not os.environ
        assert isinstance(env, dict)
        # should contain our tokens
        assert "GH_TOKEN" in env

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_subprocess_timeout(self, mock_run):
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("gh", 30)
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})
        result = engine.execute("list_repos", {}, make_gh_cred(), "{}", None)

        assert result["success"] is False
        assert result["error_code"] == "TIMEOUT"

    @patch("app.connectors.cli_engine.subprocess.run")
    def test_nonzero_exit_returns_exec_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout=b"", stderr=b"Resource not found"
        )
        manifest_path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/github_cli/adapter.json"
        )
        m, err = load_and_validate(manifest_path)
        assert err is None

        engine = CliEngine({"id": "github_cli", "backend_json": json.dumps(m.backend)})
        result = engine.execute(
            "list_issues", {"repo": "owner/repo"}, make_gh_cred(), "{}", None
        )

        assert result["success"] is False
        assert result["error_code"] == "EXEC_ERROR"
        assert "Resource not found" in result["error"]


class TestGitHubCliAdapterSeeding:
    def test_github_cli_seeds_connector_type(self, clean_db):
        from app.services import adapter_loader

        real_adapters = str(
            Path("/srv/docker-data/projects/Apps/agent-core/data/adapters").resolve()
        )
        adapter_loader.discover_and_seed_adapters(adapters_dir=real_adapters)

        from app.services import connector_service

        ct = connector_service.get_connector_type("github_cli")
        assert ct is not None, "github_cli connector_type not seeded"
        assert ct["backend_type"] == "cli"
        actions = ct.get("supported_actions", [])
        action_names = [a["name"] if isinstance(a, dict) else a for a in actions]
        for expected in ["list_repos", "list_issues", "create_issue"]:
            assert expected in action_names, f"Missing action: {expected}"
