"""Plane REST API client — session-cookie auth, paginated + deduped issue fetch."""
from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class PlaneAuthError(Exception):
    """Raised when Plane rejects the session (401/403) — session likely expired."""


class PlaneApiError(Exception):
    """Raised for other non-OK API responses."""


class PlaneClient:
    def __init__(self, settings: Settings, timeout: float = 30.0, transport=None):
        self.base_url = settings.plane_base_url
        self.workspace = settings.plane_workspace
        self.project_id = settings.plane_project_id
        self.headers = settings.plane_headers
        self.timeout = timeout
        self._transport = transport  # optional httpx transport (tests)

    # ── low-level ─────────────────────────────────────
    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(
                headers=self.headers, timeout=self.timeout,
                transport=self._transport,
            ) as client:
                r = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise PlaneApiError(f"HTTP error for {path}: {exc}") from exc
        if r.status_code in (401, 403):
            raise PlaneAuthError(f"Plane auth rejected ({r.status_code}) for {path}")
        if r.status_code >= 400:
            raise PlaneApiError(f"Plane API error {r.status_code} for {path}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as exc:
            raise PlaneApiError(f"Invalid JSON from {path}") from exc

    def _get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    # ── endpoints ─────────────────────────────────────
    def get_issues(self) -> list[dict[str, Any]]:
        """Fetch ALL issues across pages, deduplicated by id (Plane returns dupes)."""
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        base = f"/api/workspaces/{self.workspace}/projects/{self.project_id}/issues/"
        while True:
            params: dict[str, Any] = {"per_page": 50}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", base, params=params)
            batch = data.get("results") if isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise PlaneApiError(f"Unexpected issues payload shape from {base}")
            for issue in batch:
                iid = str(issue.get("id", ""))
                if iid and iid not in seen:
                    seen.add(iid)
                    results.append(issue)
            if not isinstance(data, dict):
                break  # flat list — single page
            next_cursor = data.get("next_cursor")
            has_more = data.get("has_more")
            next_page = data.get("next_page_results")
            if next_cursor:
                cursor = next_cursor
                continue
            if next_page is False or has_more is False:
                break
            if len(batch) < 50:
                break
            break  # no pagination signal; assume single page
        return results

    def get_states(self) -> dict[str, str]:
        """Return {state_id: state_name}."""
        path = f"/api/workspaces/{self.workspace}/projects/{self.project_id}/states/"
        data = self._get(path)
        batch = data.get("results") if isinstance(data, dict) else data
        out: dict[str, str] = {}
        for s in batch or []:
            sid = str(s.get("id", ""))
            name = s.get("name", "")
            if sid:
                out[sid] = name
        return out

    def get_members(self) -> dict[str, str]:
        """Return {user_id: display_name} — tolerant of member-object nesting."""
        path = f"/api/workspaces/{self.workspace}/projects/{self.project_id}/members/"
        try:
            data = self._get(path)
        except PlaneApiError:
            path2 = f"/api/workspaces/{self.workspace}/members/"
            data = self._get(path2)
        batch = data.get("results") if isinstance(data, dict) else data
        out: dict[str, str] = {}
        for m in batch or []:
            member = m.get("member") if isinstance(m, dict) else None
            src = member if isinstance(member, dict) else m
            if not isinstance(src, dict):
                continue
            uid = str(src.get("id", ""))
            if not uid:
                continue
            name = (
                src.get("display_name")
                or src.get("username")
                or src.get("first_name")
                or "?"
            )
            out[uid] = name
        return out

    def check_auth(self) -> bool:
        """Ping the API; True if session works."""
        try:
            self.get_issues()
            return True
        except PlaneAuthError:
            return False
        except PlaneApiError:
            return True  # reachable, just transient — treat as connected
