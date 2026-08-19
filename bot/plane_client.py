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
        """Fetch ALL issues across pages, deduplicated by id (Plane returns dupes).

        Supports the three pagination styles seen across Plane versions:
          1. cursor:   ?per_page=50&cursor=<next_cursor>
          2. offset:   ?per_page=50&page=<n>
          3. next-url: payload carries an absolute `next` URL
        """
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        page: int = 0
        next_url: str | None = None
        total_count: int | None = None
        pages = 0
        base = f"/api/workspaces/{self.workspace}/projects/{self.project_id}/issues/"
        while True:
            pages += 1
            if pages > 100:  # safety cap — never loop forever
                break
            if next_url:
                # absolute next URL from payload — request it directly
                try:
                    with httpx.Client(
                        headers=self.headers, timeout=self.timeout,
                        transport=self._transport,
                    ) as client:
                        r = client.get(next_url)
                    if r.status_code in (401, 403):
                        raise PlaneAuthError(f"Plane auth rejected ({r.status_code}) for next URL")
                    if r.status_code >= 400:
                        raise PlaneApiError(f"Plane API error {r.status_code} for next URL")
                    data = r.json()
                except httpx.HTTPError as exc:
                    raise PlaneApiError(f"HTTP error for next URL: {exc}") from exc
            else:
                params: dict[str, Any] = {"per_page": 50}
                if cursor:
                    params["cursor"] = cursor
                elif page:
                    params["page"] = str(page)
                data = self._request("GET", base, params=params)
            batch = data.get("results") if isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise PlaneApiError(f"Unexpected issues payload shape from {base}")
            if total_count is None and isinstance(data, dict):
                try:
                    tc = data.get("total_count") or data.get("total_results") or 0
                    total_count = int(tc) if tc else None
                except (TypeError, ValueError):
                    total_count = None
            for issue in batch:
                iid = str(issue.get("id", ""))
                if iid and iid not in seen:
                    seen.add(iid)
                    results.append(issue)
            if not isinstance(data, dict):
                break  # flat list — single page
            next_page = data.get("next_page_results")
            # Plane v1's definitive stop signal: next_page_results False.
            # NOTE: next_cursor is present on EVERY page (even the last), so it
            # must be checked AFTER this flag.
            if next_page is False:
                break
            if total_count is not None and len(results) >= total_count:
                break
            # next-URL style
            if data.get("next"):
                next_url = data["next"]
                continue
            next_cursor = data.get("next_cursor")
            if next_cursor:
                cursor = next_cursor
                continue
            # offset style: page N present and we got a full page
            if data.get("page") is not None or data.get("page_size") is not None:
                if len(batch) < 50:
                    break
                page = (data.get("page") or page) + 1
                continue
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
        """Return {user_id: display_name} — handles both live shapes:
        1. flat list: [{member: "<uuid>", role, ...}] — member is a plain UUID
        2. nested:    [{member: {id, display_name, ...}}]
        For shape 1 we can't resolve names here, so look up via workspace members
        when available, else fall back to the raw id.
        """
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
            if isinstance(member, dict):
                # nested shape
                uid = str(member.get("id", ""))
                name = (
                    member.get("display_name")
                    or member.get("username")
                    or member.get("first_name")
                    or uid
                )
                if uid:
                    out[uid] = name
            elif member:
                # flat shape — member is a plain UUID; resolve names via workspace members
                uid = str(member)
                if uid:
                    out.setdefault(uid, uid)
        # Try to resolve plain-UUID placeholders via the workspace members endpoint
        unresolved = [uid for uid, name in out.items() if name == uid]
        if unresolved:
            try:
                path3 = f"/api/workspaces/{self.workspace}/members/"
                data3 = self._get(path3)
                batch3 = data3.get("results") if isinstance(data3, dict) else data3
                for m in batch3 or []:
                    member = m.get("member") if isinstance(m, dict) else m
                    if not isinstance(member, dict):
                        continue
                    uid = str(member.get("id", ""))
                    name = (
                        member.get("display_name")
                        or member.get("username")
                        or member.get("first_name")
                        or member.get("email")
                    )
                    if uid and name and uid in out:
                        out[uid] = name
            except PlaneApiError:
                pass
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
