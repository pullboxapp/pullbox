"""Targeted unit tests for middleware coverage gaps.

Covers setup detection redirect, slow request logging, rate limit
bypass, and CSRF edge cases that aren't exercised by integration tests.

Run:
    pytest tests/unit/test_middleware_coverage.py -v
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import Response

from pullbox.api.middleware import (
    CsrfMiddleware,
    RequestLoggingMiddleware,
    SetupDetectionMiddleware,
    mark_setup_complete,
    reset_setup_cache,
)
from pullbox.database import DatabaseConnectionUnavailableError

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-middleware-tests")


class TestMarkSetupComplete:
    """Tests for mark_setup_complete() and reset_setup_cache()."""

    def test_mark_setup_complete(self) -> None:
        """mark_setup_complete should set the module-level flag."""
        import pullbox.api.middleware as mw

        reset_setup_cache()
        assert mw._setup_complete is None
        mark_setup_complete()
        assert mw._setup_complete is True
        reset_setup_cache()


@pytest.mark.parametrize("method,path", [("POST", "/library"), ("PATCH", "/api/v1/issues/17")])
async def test_offline_mutation_never_suggests_resubmitting_or_leaks_details(method, path):
    req = Request(
        {"type": "http", "method": method, "path": path, "headers": [(b"accept", b"text/html")]}
    )
    action = AsyncMock(side_effect=DatabaseConnectionUnavailableError("private host details"))
    result = await RequestLoggingMiddleware(MagicMock()).dispatch(req, action)
    assert result.status_code == 503
    assert result.headers["cache-control"] == "no-store" and "retry-after" not in result.headers
    assert b"Check the status of your last action" in result.body
    assert b"Retry this page" not in result.body and b"private host details" not in result.body
    action.assert_awaited_once()


class TestSetupDetectionRedirect:
    """Tests for SetupDetectionMiddleware when setup is not completed."""

    async def test_cold_setup_check_does_not_replay_failed_action(self) -> None:
        reset_setup_cache()
        request = Request({"type": "http", "method": "POST", "path": "/library", "headers": []})
        action = AsyncMock(side_effect=ConnectionRefusedError("unrelated downstream failure"))
        try:
            with (
                patch("pullbox.api.middleware.get_session_factory", return_value=MagicMock()),
                patch("pullbox.api.middleware.is_setup_complete_db", return_value=True),
                pytest.raises(ConnectionRefusedError),
            ):
                await SetupDetectionMiddleware(app=MagicMock()).dispatch(request, action)
            action.assert_awaited_once()
        finally:
            reset_setup_cache()

    @pytest.mark.asyncio
    async def test_redirect_html_to_setup(self) -> None:
        """HTML requests should redirect to /setup when no users exist yet."""
        reset_setup_cache()

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/dashboard",
            "query_string": b"",
            "headers": [(b"accept", b"text/html")],
        }
        request = Request(scope)

        middleware = SetupDetectionMiddleware(app=MagicMock())

        async def call_next(req: Request) -> Response:
            return Response(status_code=200)

        # Mock the DB query to return no users
        mock_factory = MagicMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("pullbox.api.middleware.get_session_factory", return_value=mock_factory):
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 302
        assert response.headers["location"] == "/setup"
        reset_setup_cache()

    @pytest.mark.asyncio
    async def test_root_html_rewrites_to_setup_path(self) -> None:
        """Root browser navigations should render the setup page in one request."""
        reset_setup_cache()

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [(b"accept", b"text/html")],
        }
        request = Request(scope)

        middleware = SetupDetectionMiddleware(app=MagicMock())
        seen_scope_path: str | None = None

        async def call_next(req: Request) -> Response:
            nonlocal seen_scope_path
            seen_scope_path = req.scope["path"]
            return Response(status_code=200)

        mock_factory = MagicMock()
        mock_session = AsyncMock()
        no_users = MagicMock()
        no_users.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=no_users)
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("pullbox.api.middleware.get_session_factory", return_value=mock_factory):
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 200
        assert seen_scope_path == "/setup"
        reset_setup_cache()

    @pytest.mark.asyncio
    async def test_existing_user_allows_request_through_without_setup_flag(self) -> None:
        """A pre-existing user should count as completed setup."""
        reset_setup_cache()

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/dashboard",
            "query_string": b"",
            "headers": [(b"accept", b"text/html")],
        }
        request = Request(scope)

        middleware = SetupDetectionMiddleware(app=MagicMock())
        call_next_called = False

        async def call_next(req: Request) -> Response:
            nonlocal call_next_called
            call_next_called = True
            return Response(status_code=200)

        mock_factory = MagicMock()
        mock_session = AsyncMock()
        existing_user = MagicMock()
        existing_user.scalar_one_or_none.return_value = 1
        mock_session.execute = AsyncMock(return_value=existing_user)
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("pullbox.api.middleware.get_session_factory", return_value=mock_factory):
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 200
        assert call_next_called is True
        reset_setup_cache()

    @pytest.mark.asyncio
    async def test_json_returns_403_setup_required(self) -> None:
        """Non-HTML, non-API requests return 403 when setup not completed."""
        reset_setup_cache()

        # Use a non-exempt, non-API path with JSON accept
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/dashboard",
            "query_string": b"",
            "headers": [(b"accept", b"application/json")],
        }
        request = Request(scope)

        middleware = SetupDetectionMiddleware(app=MagicMock())

        async def call_next(req: Request) -> Response:
            return Response(status_code=200)

        mock_factory = MagicMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("pullbox.api.middleware.get_session_factory", return_value=mock_factory):
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 403
        reset_setup_cache()

    @pytest.mark.asyncio
    async def test_db_error_passes_through(self) -> None:
        """DB errors in setup check should pass through to the app."""
        reset_setup_cache()

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/series",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)

        middleware = SetupDetectionMiddleware(app=MagicMock())
        call_next_called = False

        async def call_next(req: Request) -> Response:
            nonlocal call_next_called
            call_next_called = True
            return Response(status_code=200)

        with patch(
            "pullbox.api.middleware.get_session_factory",
            side_effect=Exception("DB error"),
        ):
            response = await middleware.dispatch(request, call_next)

        assert call_next_called
        assert response.status_code == 200
        reset_setup_cache()


class TestRequestLoggingSlow:
    """Test the slow request warning path."""

    @pytest.mark.asyncio
    async def test_slow_request_logs_warning(self) -> None:
        """Requests taking >200ms should log a warning."""
        import time

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/series",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)

        middleware = RequestLoggingMiddleware(app=MagicMock())

        async def slow_call_next(req: Request) -> Response:
            time.sleep(0.25)  # 250ms
            return Response(status_code=200)

        with patch("pullbox.api.middleware.logger") as mock_logger:
            response = await middleware.dispatch(request, slow_call_next)

        assert response.status_code == 200
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "http_request_slow"

    @pytest.mark.asyncio
    async def test_request_logging_includes_navigation_timing_breakdown(self) -> None:
        """Slow navigation logs should identify auth, sidebar, and route costs."""
        import time

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/series",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        middleware = RequestLoggingMiddleware(app=MagicMock())

        async def slow_call_next(req: Request) -> Response:
            req.state.auth_config_ms = 2.5
            req.state.sidebar_context_ms = 4.0
            req.state.series_list_query_ms = 211.25
            req.state.dashboard_intelligence_ms = 31.5
            req.state.dashboard_view_ms = 42.25
            req.state.dashboard_reading_ms = 3.75
            req.state.dashboard_render_ms = 7.0
            req.state.library_summary_query_ms = 12.5
            req.state.library_metrics_ms = 18.0
            req.state.library_catalog_ms = 91.5
            req.state.library_snapshot_ms = 35.5
            req.state.library_workspace_ms = 145.0
            req.state.library_render_ms = 5.25
            req.state.series_detail_query_ms = 88.0
            req.state.series_detail_render_ms = 6.5
            time.sleep(0.25)
            return Response(status_code=200)

        with patch("pullbox.api.middleware.logger") as mock_logger:
            await middleware.dispatch(request, slow_call_next)

        log_kwargs = mock_logger.warning.call_args.kwargs
        assert log_kwargs["auth_config_ms"] == 2.5
        assert log_kwargs["sidebar_context_ms"] == 4.0
        assert log_kwargs["series_list_query_ms"] == 211.25
        assert log_kwargs["dashboard_intelligence_ms"] == 31.5
        assert log_kwargs["dashboard_view_ms"] == 42.25
        assert log_kwargs["dashboard_reading_ms"] == 3.75
        assert log_kwargs["dashboard_render_ms"] == 7.0
        assert log_kwargs["library_summary_query_ms"] == 12.5
        assert log_kwargs["library_metrics_ms"] == 18.0
        assert log_kwargs["library_catalog_ms"] == 91.5
        assert log_kwargs["library_snapshot_ms"] == 35.5
        assert log_kwargs["library_workspace_ms"] == 145.0
        assert log_kwargs["library_render_ms"] == 5.25
        assert log_kwargs["series_detail_query_ms"] == 88.0
        assert log_kwargs["series_detail_render_ms"] == 6.5

    @pytest.mark.asyncio
    async def test_fast_server_error_logs_error(self) -> None:
        """Fast 5xx responses should log as errors, not info."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/series",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)

        middleware = RequestLoggingMiddleware(app=MagicMock())

        async def failing_call_next(req: Request) -> Response:
            return Response(status_code=500)

        with patch("pullbox.api.middleware.logger") as mock_logger:
            response = await middleware.dispatch(request, failing_call_next)

        assert response.status_code == 500
        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args
        assert call_args[0][0] == "http_request_failed"

    @pytest.mark.asyncio
    async def test_request_logging_sets_request_id_header(self) -> None:
        """Logged requests should expose the request ID on the response."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/series",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)

        middleware = RequestLoggingMiddleware(app=MagicMock())

        async def ok_call_next(req: Request) -> Response:
            return Response(status_code=200)

        response = await middleware.dispatch(request, ok_call_next)

        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == request.state.request_id

    @pytest.mark.asyncio
    async def test_fast_unauthorized_logs_warning(self) -> None:
        """Auth/security-style 4xx responses should log as warnings."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/series",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)

        middleware = RequestLoggingMiddleware(app=MagicMock())

        async def rejected_call_next(req: Request) -> Response:
            return Response(status_code=401)

        with patch("pullbox.api.middleware.logger") as mock_logger:
            response = await middleware.dispatch(request, rejected_call_next)

        assert response.status_code == 401
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "http_request_rejected"


class TestCsrfInvalidSessionFallthrough:
    """Test CSRF middleware when session token is invalid/expired."""

    @pytest.mark.asyncio
    async def test_invalid_session_passes_through(self) -> None:
        """Invalid session cookie should pass through to auth layer."""
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/series",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        request._cookies = {"pullbox_session": "invalid-token-data"}

        middleware = CsrfMiddleware(app=MagicMock())

        async def call_next(req: Request) -> Response:
            return Response(status_code=200)

        response = await middleware.dispatch(request, call_next)
        # Invalid session → CSRF middleware passes through, auth will handle it
        assert response.status_code == 200


class TestRateLimitDisabledBypass:
    """Test rate limit middleware bypass when disabled."""

    @pytest.mark.asyncio
    async def test_disabled_rate_limiter_passes_through(self) -> None:
        """Disabled rate limiter should skip all checks."""
        from pullbox.api.middleware import RateLimitMiddleware

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/series",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        request.scope["client"] = ("127.0.0.1", 12345)

        limiter = MagicMock()
        limiter.enabled = False
        middleware = RateLimitMiddleware(app=MagicMock(), limiter=limiter)

        async def call_next(req: Request) -> Response:
            return Response(status_code=200)

        response = await middleware.dispatch(request, call_next)
        assert response.status_code == 200
        # check() should NOT have been called
        limiter.check.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_pullbox_error_reraised(self) -> None:
        """Non-PullboxError exceptions should be re-raised."""
        from pullbox.api.middleware import RateLimitMiddleware

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/series",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        request.scope["client"] = ("127.0.0.1", 12345)

        limiter = MagicMock()
        limiter.enabled = True
        limiter.check = AsyncMock(side_effect=RuntimeError("unexpected"))
        middleware = RateLimitMiddleware(app=MagicMock(), limiter=limiter)

        async def call_next(req: Request) -> Response:
            return Response(status_code=200)

        with pytest.raises(RuntimeError, match="unexpected"):
            await middleware.dispatch(request, call_next)
