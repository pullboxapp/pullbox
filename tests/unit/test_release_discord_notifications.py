"""Release notification payload contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pullbox import release_discord_delivery as delivery
from pullbox.release_discord_delivery import delivery_task
from pullbox.release_discord_notifications import (
    announcement_payload,
    changelog_payload,
    notification_channels,
)

CHANGELOG = """### Added

- Added one important capability.
- Added a second important capability.

### Fixed

- Fixed a frustrating edge case.
"""


def test_final_releases_always_send_a_changelog_notification() -> None:
    assert notification_channels("1.2.3") == ("changelog",)
    assert notification_channels("1.2.0") == ("changelog", "announcements")
    assert notification_channels("2.0.0") == ("changelog", "announcements")


def test_prereleases_never_send_public_notifications() -> None:
    assert notification_channels("1.2.0-rc.1") == ()


def test_delivery_tasks_are_stable_per_channel() -> None:
    assert delivery_task("changelog") == "pullbox-discord-changelog"
    assert delivery_task("announcements") == "pullbox-discord-announcements"


def test_delivery_task_rejects_unknown_channels() -> None:
    with pytest.raises(ValueError, match="Unsupported Discord channel"):
        delivery_task("support")


def test_release_workflow_only_posts_after_a_successful_reservation() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "steps.reserve-changelog.outcome == 'success'" in workflow
    assert "steps.reserve-announcement.outcome == 'success'" in workflow
    assert "--retry-all-errors" not in workflow
    assert "PULLBOX_RELEASE_SHA: ${{ github.event.workflow_run.head_sha }}" in workflow


def test_successful_delivery_does_not_inactivate_prior_discord_records(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_request(method, url, token, payload=None):
        captured.update({"method": method, "url": url, "payload": payload})
        return {}

    monkeypatch.setattr(delivery, "_request", fake_request)

    delivery.record_delivery(
        api_url="https://api.github.test",
        repository="pullboxapp/pullbox",
        token="token",
        deployment_id=42,
        state="success",
        run_url="https://github.test/run/1",
        description="Posted Discord changelog",
    )

    assert captured["payload"] == {
        "state": "success",
        "environment": "pullbox-discord",
        "log_url": "https://github.test/run/1",
        "description": "Posted Discord changelog",
        "auto_inactive": False,
    }


def test_github_request_serializes_payload_and_headers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"id": 42}'

    def fake_urlopen(request):
        captured["request"] = request
        return Response()

    monkeypatch.setattr(delivery, "urlopen", fake_urlopen)

    assert delivery._request(
        "POST", "https://api.github.test/resource", "secret", {"value": 1}
    ) == {"id": 42}
    request = captured["request"]
    assert request.method == "POST"
    assert json.loads(request.data) == {"value": 1}
    assert request.get_header("Authorization") == "Bearer secret"


def test_delivery_reservation_creates_and_marks_an_in_progress_deployment(monkeypatch) -> None:
    calls: list[tuple[str, str, object]] = []

    def fake_request(method, url, _token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            return []
        if url.endswith("/deployments"):
            return {"id": 42}
        return {}

    monkeypatch.setattr(delivery, "_request", fake_request)

    deployment_id = delivery.reserve_delivery(
        api_url="https://api.github.test",
        repository="pullboxapp/pullbox",
        token="token",
        ref="abc123",
        run_url="https://github.test/run/1",
        version="1.2.3",
        channel="changelog",
    )

    assert deployment_id == 42
    assert calls[1][2]["payload"] == {"version": "1.2.3"}
    assert calls[2][2]["state"] == "in_progress"


@pytest.mark.parametrize("payload", [{"version": "1.2.3"}, json.dumps({"version": "1.2.3"})])
def test_delivery_reservation_skips_a_previous_success(monkeypatch, payload) -> None:
    responses = iter([[{"id": 42, "payload": payload}], [{"state": "success"}]])
    monkeypatch.setattr(delivery, "_request", lambda *_args, **_kwargs: next(responses))

    assert (
        delivery.reserve_delivery(
            api_url="https://api.github.test",
            repository="pullboxapp/pullbox",
            token="token",
            ref="abc123",
            run_url="https://github.test/run/1",
            version="1.2.3",
            channel="changelog",
        )
        is None
    )


def test_delivery_reservation_rejects_an_ambiguous_previous_attempt(monkeypatch) -> None:
    responses = iter(
        [
            [
                {"id": 42, "payload": {"version": "older"}},
                {"id": 43, "payload": {"version": "1.2.3"}},
            ],
            [{"state": "failure"}],
        ]
    )
    monkeypatch.setattr(delivery, "_request", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(RuntimeError, match="already pending or failed"):
        delivery.reserve_delivery(
            api_url="https://api.github.test",
            repository="pullboxapp/pullbox",
            token="token",
            ref="abc123",
            run_url="https://github.test/run/1",
            version="1.2.3",
            channel="announcements",
        )


def test_delivery_cli_reserves_skips_and_records_success(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        delivery,
        "os",
        SimpleNamespace(
            environ={
                "GITHUB_TOKEN": "token",
                "GITHUB_API_URL": "https://api.github.test",
                "GITHUB_REPOSITORY": "pullboxapp/pullbox",
                "GITHUB_SERVER_URL": "https://github.test",
                "GITHUB_RUN_ID": "9",
                "PULLBOX_RELEASE_SHA": "abc123",
            }
        ),
    )
    monkeypatch.setattr(delivery, "reserve_delivery", lambda **_kwargs: None)
    assert delivery.main(["reserve", "--channel", "changelog", "--version", "1.2.3"]) == 0
    assert capsys.readouterr().out.strip() == "skip=true"

    monkeypatch.setattr(delivery, "reserve_delivery", lambda **_kwargs: 42)
    assert delivery.main(["reserve", "--channel", "changelog", "--version", "1.2.3"]) == 0
    assert capsys.readouterr().out.strip() == "deployment_id=42"

    recorded: dict[str, object] = {}
    monkeypatch.setattr(delivery, "record_delivery", lambda **kwargs: recorded.update(kwargs))
    assert (
        delivery.main(
            [
                "success",
                "--channel",
                "announcements",
                "--version",
                "1.2.3",
                "--deployment-id",
                "42",
            ]
        )
        == 0
    )
    assert recorded["deployment_id"] == 42
    assert recorded["description"] == "Posted Discord announcements"


def test_delivery_cli_requires_deployment_id_for_success(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "pullboxapp/pullbox")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.test")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")

    with pytest.raises(SystemExit, match="2"):
        delivery.main(["success", "--channel", "changelog", "--version", "1.2.3"])


def test_changelog_payload_is_an_embed_with_mentions_disabled() -> None:
    payload = changelog_payload(
        "1.2.3", CHANGELOG, "https://github.com/pullboxapp/pullbox/releases/tag/v1.2.3"
    )

    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["embeds"][0]["title"] == "Pullbox v1.2.3 changelog"
    assert "Added one important capability." in payload["embeds"][0]["description"]
    assert payload["embeds"][0]["url"].endswith("v1.2.3")


def test_announcement_payload_is_short_and_links_to_the_release() -> None:
    payload = announcement_payload(
        "1.2.0", CHANGELOG, "https://github.com/pullboxapp/pullbox/releases/tag/v1.2.0"
    )

    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["embeds"][0]["title"] == "Pullbox v1.2.0 is out"
    assert "Added one important capability." in payload["embeds"][0]["description"]
    assert "Fixed a frustrating edge case." not in payload["embeds"][0]["description"]
    assert "Read the full release notes" in payload["embeds"][0]["description"]
