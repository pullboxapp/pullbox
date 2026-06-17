"""Compatibility helpers for FastAPI route contract tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from typing import Any


class RouteContract(Protocol):
    """Small route shape used by tests that introspect FastAPI routes."""

    path: str
    methods: set[str]
    name: str
    endpoint: Callable[..., Any]
    dependant: Any


def iter_api_route_contracts(routes: Iterable[object]) -> Iterator[RouteContract]:
    """Yield API route-like objects across FastAPI router storage variants.

    FastAPI 0.137 can retain included routers as wrapper objects instead of
    eagerly flattening them into top-level ``APIRoute`` instances. Contract
    tests care about the effective routes, so expand those wrappers when the
    framework exposes them.
    """

    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue

        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if callable(effective_route_contexts):
            yield from effective_route_contexts()
