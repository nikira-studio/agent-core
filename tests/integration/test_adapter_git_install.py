"""Wire-level tests for git:owner/repo@ref adapter installation."""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.services.adapter_loader import (
    install_from_git,
    AdapterInstallError,
    GIT_SOURCE_RE,
)


class TestGitSourceRegex:
    def test_parses_valid_source(self):
        match = GIT_SOURCE_RE.match("git:acme/github@main")
        assert match is not None
        assert match.group("owner") == "acme"
        assert match.group("repo") == "github"
        assert match.group("ref") == "main"

    def test_parses_with_version_ref(self):
        match = GIT_SOURCE_RE.match("git:owner/repo@v1.2.3")
        assert match is not None
        assert match.group("owner") == "owner"
        assert match.group("repo") == "repo"
        assert match.group("ref") == "v1.2.3"

    def test_rejects_invalid_source(self):
        assert GIT_SOURCE_RE.match("https://github.com/owner/repo") is None
        assert GIT_SOURCE_RE.match("owner/repo@main") is None
        assert GIT_SOURCE_RE.match("git:owner/repo") is None
        assert GIT_SOURCE_RE.match("git:owner/repo@") is None


class TestDangerousPatternScanner:
    def test_detects_cred_raw_block(self):
        from app.security.dangerous_pattern_scanner import contains_dangerous_patterns

        manifest = '{"actions": [{"params": {"token": "{{ cred.raw }}"}}]}'
        assert contains_dangerous_patterns(manifest) is True

    def test_detects_env_var_injection(self):
        from app.security.dangerous_pattern_scanner import contains_dangerous_patterns

        manifest = '{"env": {"API_KEY": "${SECRET_KEY}"}}'
        assert contains_dangerous_patterns(manifest) is True

    def test_detects_command_substitution(self):
        from app.security.dangerous_pattern_scanner import contains_dangerous_patterns

        manifest = '{"actions": [{"argv": ["$(whoami)"]}]}'
        assert contains_dangerous_patterns(manifest) is True

    def test_detects_shell_chaining_and(self):
        from app.security.dangerous_pattern_scanner import contains_dangerous_patterns

        manifest = '{"actions": [{"argv": ["echo", "a&&b"]}]}'
        assert contains_dangerous_patterns(manifest) is True

    def test_detects_shell_chaining_or(self):
        from app.security.dangerous_pattern_scanner import contains_dangerous_patterns

        manifest = '{"actions": [{"argv": ["echo", "a||b"]}]}'
        assert contains_dangerous_patterns(manifest) is True

    def test_detects_path_traversal(self):
        from app.security.dangerous_pattern_scanner import contains_dangerous_patterns

        manifest = '{"env": {"PATH": "../../../etc/passwd"}}'
        assert contains_dangerous_patterns(manifest) is True

    def test_safe_manifest_passes(self):
        from app.security.dangerous_pattern_scanner import contains_dangerous_patterns

        manifest = json.dumps(
            {
                "id": "test",
                "version": "1.0.0",
                "display_name": "Test",
                "actions": [
                    {
                        "name": "list_items",
                        "method": "GET",
                        "path": "/items",
                        "params": {"limit": {"type": "integer"}},
                    }
                ],
            }
        )
        assert contains_dangerous_patterns(manifest) is False


class TestInstallFromGit:
    @patch("subprocess.run")
    def test_parses_git_source_and_clones(self, mock_run):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "git" and cmd[1] == "clone":
                dest = Path(cmd[-1])
                (dest / "adapter.json").write_text(
                    '{"id": "owner_repo", "version": "1.0.0"}'
                )
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = fake_run

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.services.adapter_loader.settings") as mock_settings:
                mock_settings.data_dir = tmpdir

                result = install_from_git("git:owner/repo@main")

        assert result == "owner_repo"
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        argv = call_args[0][0]
        assert argv[0:7] == [
            "git",
            "clone",
            "--depth",
            "1",
            "-b",
            "main",
            "https://github.com/owner/repo.git",
        ]
        assert len(argv) == 8
        assert Path(argv[7]).is_absolute()
        assert call_args[1]["timeout"] == 60

    @patch("subprocess.run")
    def test_rejects_dangerous_patterns(self, mock_run):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "git" and cmd[1] == "clone":
                dest = Path(cmd[-1])
                dangerous_manifest = '{"id": "evil", "{{ cred.raw }}": "bad"}'
                (dest / "adapter.json").write_text(dangerous_manifest)
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = fake_run

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.services.adapter_loader.settings") as mock_settings:
                mock_settings.data_dir = tmpdir

                with pytest.raises(AdapterInstallError) as exc_info:
                    install_from_git("git:owner/repo@main")

        assert "Dangerous patterns detected" in str(exc_info.value)

    @patch("subprocess.run")
    def test_rejects_missing_adapter_json(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter_dir = Path(tmpdir) / "owner_repo"
            adapter_dir.mkdir()

            with patch("app.services.adapter_loader.settings") as mock_settings:
                mock_settings.data_dir = tmpdir

                with pytest.raises(AdapterInstallError) as exc_info:
                    install_from_git("git:owner/repo@main")

        assert "adapter.json not found" in str(exc_info.value)

    def test_invalid_source_format_raises_error(self):
        with pytest.raises(AdapterInstallError) as exc_info:
            install_from_git("not-a-valid-source")

        assert "Invalid git source format" in str(exc_info.value)

    @patch("subprocess.run")
    def test_clone_timeout_raises_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("git clone", 60)

        with pytest.raises(AdapterInstallError) as exc_info:
            install_from_git("git:owner/repo@main")

        assert "timed out" in str(exc_info.value)

    @patch("subprocess.run")
    def test_clone_failure_raises_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "git clone", stderr="Repository not found"
        )

        with pytest.raises(AdapterInstallError) as exc_info:
            install_from_git("git:owner/repo@main")

        assert "Git clone failed" in str(exc_info.value)
