"""Thin client for the Supabase Management API."""

from __future__ import annotations

import time
from typing import Any

import httpx

from supabase_charter.errors import AgentError, ProjectNotFoundError

API_ROOT = "https://api.supabase.com"
HEALTHY = "ACTIVE_HEALTHY"
# Statuses a project shows once a deletion has been accepted.
GONE = frozenset({"REMOVED", "GOING_DOWN"})


def project_gone(project: dict[str, Any]) -> bool:
    """True when the listing still shows the project but it is being removed."""
    return str(project.get("status", "")).upper() in GONE


class SupabaseApi:
    def __init__(self, token_provider, http: httpx.Client | None = None) -> None:
        """token_provider(force: bool) -> (access_token, org_slug)."""
        self._token_provider = token_provider
        self._http = http or httpx.Client(timeout=60)

    def list_projects(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/v1/projects")
        return payload if isinstance(payload, list) else []

    def create_project(
        self,
        *,
        name: str,
        organization_slug: str,
        db_pass: str,
        region: str,
        instance_size: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/projects",
            json={
                "name": name,
                "organization_slug": organization_slug,
                "db_pass": db_pass,
                "region": region,
                "desired_instance_size": instance_size,
            },
        )

    def get_project(self, ref: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/projects/{ref}")

    def wait_until_healthy(
        self, ref: str, *, attempts: int = 60, delay_seconds: float = 10
    ) -> dict[str, Any]:
        last = ""
        for _ in range(attempts):
            project = self.get_project(ref)
            last = str(project.get("status", ""))
            if last == HEALTHY:
                return project
            time.sleep(delay_seconds)
        raise AgentError(
            f"Project {ref} did not become {HEALTHY} (last status: {last or 'unknown'})."
        )

    def api_keys(self, ref: str) -> dict[str, str]:
        payload = self._request("GET", f"/v1/projects/{ref}/api-keys")
        rows = (
            payload
            if isinstance(payload, list)
            else payload.get("keys", payload.get("api_keys", []))
        )
        found: dict[str, str] = {}
        for row in rows:
            name = str(row.get("name", "")).lower()
            value = row.get("api_key") or row.get("apiKey") or ""
            if name in {"anon", "service_role"} and value:
                found[name] = value
        return found

    def pooler(self, ref: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/projects/{ref}/config/database/pooler")

    def update_password(self, ref: str, password: str) -> None:
        self._request(
            "PATCH",
            f"/v1/projects/{ref}/database/password",
            json={"password": password},
        )

    def delete_project(self, ref: str) -> None:
        self._request("DELETE", f"/v1/projects/{ref}")

    def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        response = None
        for attempt in (1, 2):
            token, _org = self._token_provider(force=attempt == 2)
            response = self._http.request(
                method,
                f"{API_ROOT}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=json,
            )
            if response.status_code == 401 and attempt == 1:
                continue
            break
        assert response is not None
        if response.status_code >= 400:
            message = f"Supabase API {method} {path} failed ({response.status_code}): {response.text[:500]}"
            if response.status_code == 404:
                raise ProjectNotFoundError(message)
            raise AgentError(message)
        if not response.content:
            return {}
        return response.json()
