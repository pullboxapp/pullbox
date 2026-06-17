"""Route access matrix tests for API exposure hardening."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pullbox.api.deps import require_auth, require_interactive_auth, require_stream_auth
from pullbox.app import create_app
from tests.route_contracts import RouteContract, iter_api_route_contracts

if TYPE_CHECKING:
    from collections.abc import Iterable

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-route-matrix")


def _iter_dependency_calls(route: RouteContract) -> Iterable[object]:
    stack = list(route.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        yield dependency.call
        stack.extend(dependency.dependencies)


def _classify_route(route: RouteContract) -> str:
    calls = set(_iter_dependency_calls(route))
    if require_interactive_auth in calls:
        return "interactive"
    if require_auth in calls:
        return "authenticated"
    if require_stream_auth in calls:
        return "authenticated"
    return "public"


class TestApiRouteAccessMatrix:
    """Public API exposure should remain tightly controlled."""

    def test_public_api_allowlist_is_exact(self) -> None:
        app = create_app()

        public_routes: set[tuple[str, str]] = set()
        for route in iter_api_route_contracts(app.routes):
            if not route.path.startswith("/api/v1"):
                continue
            if _classify_route(route) != "public":
                continue
            for method in route.methods - {"HEAD", "OPTIONS"}:
                public_routes.add((method, route.path))

        assert public_routes == {
            ("GET", "/api/v1/auth/password-policy"),
            ("POST", "/api/v1/auth/login"),
            ("POST", "/api/v1/auth/logout"),
            ("POST", "/api/v1/system/setup"),
        }

    def test_representative_route_classes_match_policy(self) -> None:
        app = create_app()
        route_map = {
            (method, route.path): _classify_route(route)
            for route in iter_api_route_contracts(app.routes)
            for method in (route.methods - {"HEAD", "OPTIONS"})
            if route.path.startswith("/api/v1")
        }

        assert route_map[("GET", "/api/v1/series")] == "authenticated"
        assert route_map[("GET", "/api/v1/system/about")] == "interactive"
        assert route_map[("GET", "/api/v1/utilities/jobs/{job_id}/stream")] == "interactive"
