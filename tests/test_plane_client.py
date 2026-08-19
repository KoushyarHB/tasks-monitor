"""Tests for bot.plane_client — auth, pagination dedup, normalization."""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import Settings
from bot.plane_client import PlaneAuthError, PlaneApiError, PlaneClient


def make_client(handler):
    settings = Settings(
        plane_base_url="https://plane.test",
        plane_workspace="tms",
        plane_project_id="proj1",
        plane_csrf_token="csrf",
        plane_session_id="sess",
    )
    transport = httpx.MockTransport(handler)
    return PlaneClient(settings, transport=transport)


def _resp(data, status=200):
    return httpx.Response(status, json=data)


def test_auth_headers_sent():
    captured = {}

    def handler(request):
        captured["cookie"] = request.headers.get("cookie", "")
        captured["csrf"] = request.headers.get("x-csrftoken", "")
        return _resp({"results": []})

    c = make_client(handler)
    c.get_issues()
    assert "csrftoken=csrf" in captured["cookie"]
    assert "session-id=sess" in captured["cookie"]
    assert captured["csrf"] == "csrf"


def test_401_raises_auth_error():
    def handler(request):
        return httpx.Response(401, json={"detail": "no"})

    c = make_client(handler)
    with pytest.raises(PlaneAuthError):
        c.get_issues()


def test_403_raises_auth_error():
    def handler(request):
        return httpx.Response(403, json={"detail": "no"})

    c = make_client(handler)
    with pytest.raises(PlaneAuthError):
        c.get_issues()


def test_500_raises_api_error():
    def handler(request):
        return httpx.Response(500, json={})

    c = make_client(handler)
    with pytest.raises(PlaneApiError):
        c.get_issues()


def test_dedup_across_pages():
    pages = iter([
        {"results": [
            {"id": "a1", "name": "one"},
            {"id": "a2", "name": "two"},
        ], "next_cursor": "50:1:0", "next_page_results": True},
        {"results": [
            {"id": "a2", "name": "two"},   # duplicate!
            {"id": "a3", "name": "three"},
        ], "next_cursor": None, "next_page_results": False},
    ])

    def handler(request):
        return _resp(next(pages))

    c = make_client(handler)
    issues = c.get_issues()
    ids = [i["id"] for i in issues]
    assert len(ids) == 3
    assert ids == ["a1", "a2", "a3"]  # deduped, order preserved


def test_cursor_pagination_stops_on_total_count():
    """Live Plane payloads carry total_count + next_cursor (e.g. '50:1:0');
    loop must stop once accumulated results reach total_count even when
    next_page_results stays True."""
    pages = iter([
        {"results": [{"id": f"a{i}"} for i in range(50)],
         "next_cursor": "50:1:0", "next_page_results": True, "total_count": 120},
        {"results": [{"id": f"b{i}"} for i in range(50)],
         "next_cursor": "50:2:0", "next_page_results": True, "total_count": 120},
        {"results": [{"id": f"c{i}"} for i in range(20)],
         "next_cursor": None, "next_page_results": False, "total_count": 120},
    ])

    def handler(request):
        return _resp(next(pages))

    c = make_client(handler)
    issues = c.get_issues()
    assert len(issues) == 120
    assert len({i["id"] for i in issues}) == 120  # all unique


def test_cursor_pagination_stops_when_batch_short():
    """If next_page_results absent/missing, fall back to short-batch stop."""
    pages = iter([
        {"results": [{"id": f"a{i}"} for i in range(50)], "next_cursor": "50:1:0"},
        {"results": [{"id": f"b{i}"} for i in range(30)], "next_cursor": None},
    ])

    def handler(request):
        return _resp(next(pages))

    c = make_client(handler)
    assert len(c.get_issues()) == 80


def test_flat_list_single_page():
    def handler(request):
        return _resp([{"id": "x1"}, {"id": "x2"}])

    c = make_client(handler)
    assert len(c.get_issues()) == 2


def test_states_normalized():
    def handler(request):
        return _resp({"results": [
            {"id": "s1", "name": "Backlog"},
            {"id": "s2", "name": "Code Review"},
        ]})

    c = make_client(handler)
    assert c.get_states() == {"s1": "Backlog", "s2": "Code Review"}


def test_members_normalized_nested():
    def handler(request):
        return _resp({"results": [
            {"member": {"id": "u1", "display_name": "Koushyar Heidari"}},
            {"member": {"id": "u2", "username": "feizyr"}},
        ]})

    c = make_client(handler)
    assert c.get_members() == {"u1": "Koushyar Heidari", "u2": "feizyr"}


def test_members_flat_uuid_shape_with_workspace_resolution():
    """Live Plane v1 returns flat [{member: '<uuid>', ...}]; names are resolved
    by a second call to the workspace members endpoint (nested dicts)."""
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if "/projects/proj1/members/" in str(request.url):
            return _resp([
                {"member": "u1", "role": 15},
                {"member": "u2", "role": 15},
            ])
        if "/workspaces/tms/members/" in str(request.url):
            return _resp({"results": [
                {"member": {"id": "u1", "display_name": "Koushyar Heidari"}},
                {"member": {"id": "u2", "display_name": "feizyr"}},
            ]})
        return _resp({"results": []})

    c = make_client(handler)
    members = c.get_members()
    assert members == {"u1": "Koushyar Heidari", "u2": "feizyr"}
    assert len(requests) == 2  # project members + workspace members lookup
