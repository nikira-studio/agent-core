"""Declarative CLI engine for adapter manifests."""

import json
import re
import subprocess
from typing import Any, Optional

from app.connectors import BaseConnector
from app.connectors.base import Credential

_RE_TEMPLATE = re.compile(
    r"\{\{\s*(params|cred|config)(?:\.(\w+?))?\s*(?:\s+\|\s*(\w+)(?:\(([^)]*)\))?)?\s*\}\}"
)

_OMIT_SENTINEL = "__OMIT__"


class CliEngine(BaseConnector):
    def __init__(self, connector_type: dict):
        self.ct = connector_type
        raw = connector_type.get("backend_json", "{}")
        if isinstance(raw, str):
            self.spec = json.loads(raw)
        else:
            self.spec = raw
        self.bin = self.spec.get("bin", "")
        self.default_timeout = self.spec.get("timeout", 30)
        self.env_spec = self.spec.get("env", {})
        self.commands = self.spec.get("commands", {})

    def execute(
        self,
        action: str,
        params: dict,
        credential: Credential,
        config_json: Optional[str],
        session: Optional[dict] = None,
    ) -> dict:
        cmd_def = self.commands.get(action)
        if not cmd_def:
            return {
                "success": False,
                "error": f"No command defined for action: {action}",
            }

        config = _parse_json(config_json)

        env = self._build_env(params, config, credential)
        argv = self._build_argv(cmd_def, params, config, credential)
        timeout = cmd_def.get("timeout", self.default_timeout)

        stdin_content = cmd_def.get("stdin")
        stdin_bytes = None
        if stdin_content:
            rendered_stdin = self._render(stdin_content, params, config, credential)
            stdin_bytes = rendered_stdin.encode("utf-8") if rendered_stdin else None

        try:
            result = subprocess.run(
                [self.bin] + argv,
                env=env,
                stdin=subprocess.PIPE if stdin_bytes else None,
                input=stdin_bytes,
                capture_output=True,
                shell=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error_code": "TIMEOUT",
                "error": f"Command timed out after {timeout} seconds",
            }

        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")

        success_codes = cmd_def.get("success_exit_codes", [0])
        if result.returncode not in success_codes:
            err_msg = stderr.strip() if stderr.strip() else f"exit {result.returncode}"
            return {
                "success": False,
                "error_code": "EXEC_ERROR",
                "error": err_msg,
            }

        return self._parse_output(cmd_def, stdout)

    def refresh_session(
        self,
        credential: Credential,
        config_json: Optional[str],
        current_session: Optional[dict],
    ) -> dict:
        return {"error": "cli does not support session refresh"}

    def _build_env(
        self, params: dict, config: dict, cred: Optional[Credential]
    ) -> dict:
        env = {}
        for key, tpl in self.env_spec.items():
            env[key] = self._render(tpl, params, config, cred)
        return env

    def _build_argv(
        self,
        cmd_def: dict,
        params: dict,
        config: dict,
        cred: Optional[Credential],
    ) -> list[str]:
        args_tpl = cmd_def.get("args", [])
        argv = []
        for item in args_tpl:
            rendered = self._render(item, params, config, cred)
            if rendered == "" or rendered == _OMIT_SENTINEL:
                argv.append(_OMIT_SENTINEL)
            else:
                argv.append(rendered)
        return self._apply_omit_sentinel(argv)

    def _apply_omit_sentinel(self, argv: list[str]) -> list[str]:
        result = []
        skip_next = False
        for i, item in enumerate(argv):
            if skip_next:
                skip_next = False
                continue
            if item == _OMIT_SENTINEL:
                if result and result[-1].startswith("-"):
                    result.pop()
                    skip_next = False
                continue
            result.append(item)
        return result

    def _render(
        self,
        template: str,
        params: dict,
        config: dict,
        cred: Optional[Credential] = None,
    ) -> str:
        def replacer(m):
            src, key = m.group(1), m.group(2)
            filter_name = m.group(3)
            filter_arg = m.group(4)

            def _get_value():
                if src == "params":
                    if key:
                        return params.get(key, m.group(0))
                    return params
                if src == "cred":
                    if key:
                        return self._cred_get(key, params, config, cred)
                    return params
                if src == "config":
                    if key:
                        return config.get(key, m.group(0))
                    return params
                return m.group(0)

            val = _get_value()
            if filter_name == "default":
                if val is None or val == m.group(0):
                    type_map = {
                        "str": "",
                        "int": 0,
                        "float": 0.0,
                        "bool": False,
                        "list": [],
                        "omit": _OMIT_SENTINEL,
                    }
                    fallback_str = ""
                    type_arg = filter_arg
                    if filter_arg and ", as=" in filter_arg:
                        parts = filter_arg.split(", as=", 1)
                        fallback_str = parts[0]
                        type_arg = parts[1] if len(parts) > 1 else ""
                    if type_arg == "omit":
                        return _OMIT_SENTINEL
                    if fallback_str:
                        try:
                            return str(json.loads(fallback_str))
                        except (json.JSONDecodeError, ValueError):
                            return fallback_str
                    if type_arg and type_arg in type_map:
                        return str(type_map[type_arg])
                    return ""
                return str(val)
            if val is None:
                return m.group(0)
            return str(val)

        return _RE_TEMPLATE.sub(replacer, template)

    def _cred_get(
        self, key: str, params: dict, config: dict, cred: Optional[Credential] = None
    ) -> Any:
        parts = key.split(".", 1)
        field = parts[0]
        sub = parts[1] if len(parts) > 1 else None

        if field == "raw":
            return cred.raw if cred is not None else params.get("_cred", {}).get("raw")

        if field == "base64_credentials":
            username = self._cred_get("username", params, config, cred) or ""
            password = self._cred_get("password", params, config, cred) or ""
            import base64

            return base64.b64encode(f"{username}:{password}".encode()).decode()

        if cred is not None and field in cred.fields:
            val = cred.fields[field]
            if sub and isinstance(val, dict):
                return val.get(sub)
            return val

        val = params.get("_cred", {}).get(field) or config.get(field)
        if sub and isinstance(val, dict):
            return val.get(sub)
        return val

    def _parse_output(self, cmd_def: dict, stdout: str) -> dict:
        parse_spec = cmd_def.get("parse", {})
        parse_type = parse_spec.get("type", "text")

        if parse_type == "text":
            return {"success": True, "output": stdout}

        if parse_type == "jsonpath":
            try:
                body = json.loads(stdout)
            except json.JSONDecodeError:
                return {"success": False, "error": "Failed to parse JSON output"}

            path = parse_spec.get("path", "$")
            extracted = self._extract_jsonpath(path, body)
            return {"success": True, "output": extracted}

        if parse_type == "regex":
            pattern = parse_spec.get("path", "")
            group_idx = parse_spec.get("group", 0)
            m = re.search(pattern, stdout)
            if not m:
                return {
                    "success": False,
                    "error": f"Regex pattern '{pattern}' not found",
                }
            groups = m.groups()
            if groups:
                result = groups[group_idx] if group_idx < len(groups) else groups[0]
                return {"success": True, "output": result}
            return {"success": True, "output": m.group(0)}

        return {"success": True, "output": stdout}

    def _extract_jsonpath(self, path: str, body: Any) -> Any:
        if path.startswith("$."):
            parts = path[2:].split(".")
            current = body
            for part in parts:
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    return None
            return current
        return body


def _parse_json(config_json: Optional[str]) -> dict:
    if not config_json:
        return {}
    try:
        val = json.loads(config_json)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}
