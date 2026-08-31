"""Arc redirects use local routes, validated identifiers, and encoded query values."""

from urllib.parse import urlsplit

import pytest
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request

from pullbox.ui import story_arc_catalog_routes, story_arc_routes


@pytest.mark.parametrize(
    "value",
    [
        "//example.com",
        "https://example.com",
        "1/../../",
        "1\\evil",
        "%2f%2fexample.com",
        "42?next=//example.com",
    ],
)
def test_catalog_provider_id_rejects_url_components(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(story_arc_catalog_routes._ProviderId).validate_python(value)


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize("catalog", [False, True])
@pytest.mark.parametrize(
    "message",
    ["//example.com", "https://example.com/#x", "\\\\example.com", "\r\nLocation: //example.com"],
)
def test_arc_redirects_keep_encoded_messages_on_local_route(
    htmx: bool, catalog: bool, message: str
) -> None:
    request = Request({"type": "http", "headers": [(b"hx-request", b"true")] if htmx else []})
    destination = story_arc_routes._detail_url(42, error=message, page=2, per_page=25)
    redirect = story_arc_catalog_routes._redirect if catalog else story_arc_routes._redirect
    response = redirect(request, destination)
    assert response.status_code == (204 if htmx else 303)
    location = response.headers["HX-Redirect" if htmx else "Location"]
    parsed = urlsplit(location)
    assert parsed.scheme == parsed.netloc == ""
    assert parsed.path == "/story-arcs/42"
    assert "page=2" in parsed.query
    assert "\r" not in location and "\n" not in location
