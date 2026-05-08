import json
import re
import time
import urllib.request
import urllib.error
from typing import Optional


class GitHubConnector:
    connector_type_id = "github"
    base_url = "https://api.github.com"

    def test_connection(self, credential: str, config_json: Optional[str]) -> dict:
        try:
            req = urllib.request.Request(
                f"{self.base_url}/user",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    user = json.loads(resp.read())
                    return {"success": True, "user": user.get("login")}
                return {"success": False, "error": f"Unexpected status: {resp.status}"}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return {"success": False, "error": f"HTTP {e.code}: {body[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def execute(
        self, action: str, params: dict, credential: str, config_json: Optional[str]
    ) -> dict:
        if action == "create_issue":
            return self._create_issue(credential, params)
        elif action == "comment_issue":
            return self._comment_issue(credential, params)
        elif action == "read_repo":
            return self._read_repo(credential, params)
        else:
            return {"success": False, "error": f"Unknown action: {action}"}

    def _headers(self, credential: str) -> dict:
        return {
            "Authorization": f"Bearer {credential}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def _do(
        self, method: str, path: str, credential: str, data: Optional[dict] = None
    ) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(
            url, method=method, headers=self._headers(credential), data=body
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_body = resp.read()
                return resp.status, json.loads(resp_body) if resp_body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {"error": body[:500]}

    def _create_issue(self, credential: str, params: dict) -> dict:
        owner = params.get("owner")
        repo = params.get("repo")
        title = params.get("title")
        if not owner or not repo or not title:
            return {"success": False, "error": "owner, repo, and title are required"}
        body = params.get("body", "")
        labels = params.get("labels")
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels if isinstance(labels, list) else [labels]
        status, data = self._do(
            "POST", f"/repos/{owner}/{repo}/issues", credential, payload
        )
        if status in (200, 201):
            return {
                "success": True,
                "issue_url": data.get("html_url"),
                "issue_number": data.get("number"),
                "issue_id": data.get("id"),
            }
        error = data.get("error") or data.get("message", str(data))
        return {"success": False, "error": f"GitHub API {status}: {error}"}

    def _comment_issue(self, credential: str, params: dict) -> dict:
        owner = params.get("owner")
        repo = params.get("repo")
        issue_number = params.get("issue_number")
        body = params.get("body")
        if not owner or not repo or not issue_number or not body:
            return {
                "success": False,
                "error": "owner, repo, issue_number, and body are required",
            }
        status, data = self._do(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            credential,
            {"body": body},
        )
        if status in (200, 201):
            return {
                "success": True,
                "comment_url": data.get("html_url"),
                "comment_id": data.get("id"),
            }
        error = data.get("error") or data.get("message", str(data))
        return {"success": False, "error": f"GitHub API {status}: {error}"}

    def _read_repo(self, credential: str, params: dict) -> dict:
        owner = params.get("owner")
        repo = params.get("repo")
        if not owner or not repo:
            return {"success": False, "error": "owner and repo are required"}
        status, data = self._do("GET", f"/repos/{owner}/{repo}", credential)
        if status == 200:
            return {
                "success": True,
                "repo": {
                    "full_name": data.get("full_name"),
                    "description": data.get("description"),
                    "stars": data.get("stargazers_count"),
                    "forks": data.get("forks_count"),
                    "language": data.get("language"),
                    "open_issues": data.get("open_issues_count"),
                    "created_at": data.get("created_at"),
                    "pushed_at": data.get("pushed_at"),
                },
            }
        error = data.get("error") or data.get("message", str(data))
        return {"success": False, "error": f"GitHub API {status}: {error}"}


from app.connectors import register_connector

register_connector("github", GitHubConnector)
