"""
Pullbox API middleware — security headers, CSRF, logging, rate limiting, and setup.

Adds security response headers to all responses, enforces CSRF token
validation on state-changing requests, logs every HTTP request with
method, path, status code, and duration using structured logging.
Enforces per-IP rate limits by endpoint tier. Redirects unauthenticated
requests to the setup page when no users have been created.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request  # noqa: TC002 — used at runtime in dispatch()
from starlette.responses import JSONResponse, RedirectResponse, Response

from pullbox.config import get_settings
from pullbox.core.local_auth_bypass import LocalAuthBypassResolution, resolve_local_auth_bypass
from pullbox.database import DatabaseConnectionUnavailableError, get_session_factory
from pullbox.models.user import User
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.core.api_rate_limiter import APIRateLimiter

logger = structlog.get_logger(__name__)
_SLOW_REQUEST_THRESHOLD_MS = 200.0
_REQUEST_TIMING_FIELDS = (
    "auth_config_ms",
    "sidebar_context_ms",
    "series_list_query_ms",
    "series_list_render_ms",
    "dashboard_intelligence_ms",
    "dashboard_view_ms",
    "dashboard_reading_ms",
    "dashboard_render_ms",
    "library_summary_query_ms",
    "library_metrics_ms",
    "library_catalog_ms",
    "library_snapshot_ms",
    "library_workspace_ms",
    "library_render_ms",
    "series_detail_query_ms",
    "series_detail_render_ms",
)

# Module-level cache for setup completion status
_setup_complete: bool | None = None

# Paths exempt from setup detection.
# API paths are exempt because they have their own auth via FastAPI dependencies.
_SETUP_EXEMPT_PREFIXES = (
    "/setup",
    "/login",
    "/api/",
    "/static/",
    "/ping",
    "/health",
    "/favicon.ico",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def _request_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the app-scoped session factory when available."""
    app = request.scope.get("app")
    app_state = getattr(app, "state", None)
    factory = getattr(app_state, "db_session_factory", None)
    return factory or get_session_factory()


def mark_setup_complete() -> None:
    """Mark setup as complete, called after first user is created."""
    global _setup_complete
    _setup_complete = True


def is_setup_complete() -> bool | None:
    """Return the cached setup-complete flag (``True``, ``False``, or ``None`` if unknown)."""
    return _setup_complete


def reset_setup_cache() -> None:
    """Reset the setup cache to None. Used in tests."""
    global _setup_complete
    _setup_complete = None


class SetupDetectionMiddleware(BaseHTTPMiddleware):
    """Redirect to setup page until the first account has been created."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        global _setup_complete

        path = request.url.path

        # Skip exempt paths (API, static, login, setup page itself, etc.)
        if any(path.startswith(prefix) for prefix in _SETUP_EXEMPT_PREFIXES):
            return await call_next(request)

        # Check cached value first
        if _setup_complete:
            return await call_next(request)

        # Query setup state from the database when the cache is cold.
        try:
            factory = _request_session_factory(request)
            async with factory() as session:
                complete = await is_setup_complete_db(session)
        except DatabaseConnectionUnavailableError:
            raise
        except Exception:
            logger.exception("setup_detection_db_error", path=request.url.path)
            return await call_next(request)
        if complete:
            _setup_complete = True
            return await call_next(request)

        # Setup not complete — redirect HTML requests to /setup
        accepts = request.headers.get("accept", "")
        is_html = "text/html" in accepts and "application/json" not in accepts
        is_htmx = request.headers.get("hx-request") == "true"

        if is_html and path == "/":
            request.scope["path"] = "/setup"
            request.scope["raw_path"] = b"/setup"
            return await call_next(request)

        if is_html or is_htmx:
            return RedirectResponse(url="/setup", status_code=302)

        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "SETUP_REQUIRED",
                    "message": "First-run setup has not been completed.",
                }
            },
        )


async def is_setup_complete_db(session: AsyncSession) -> bool:
    """Return True when setup is effectively complete for this database.

    Setup is complete once at least one user already exists.
    """
    user_result = await session.execute(select(User.id).limit(1))
    return user_result.scalar_one_or_none() is not None


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, status code, and duration for every request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip correlation ID binding for static assets
        if request.url.path.startswith("/static"):
            return await call_next(request)

        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            start = time.monotonic()
            try:
                response = await call_next(request)
            except DatabaseConnectionUnavailableError:
                response = _database_unavailable_response(request)
            duration_ms = (time.monotonic() - start) * 1000
            response.headers["X-Request-ID"] = request_id

            log_kwargs = {
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
                "request_id": request_id,
            }
            for timing_key in _REQUEST_TIMING_FIELDS:
                timing_value = getattr(request.state, timing_key, None)
                if isinstance(timing_value, int | float):
                    log_kwargs[timing_key] = timing_value
            if response.status_code >= 500:
                logger.error("http_request_failed", outcome="server_error", **log_kwargs)
            elif duration_ms > _SLOW_REQUEST_THRESHOLD_MS:
                outcome = "client_error" if response.status_code >= 400 else "success"
                logger.warning("http_request_slow", outcome=outcome, **log_kwargs)
            elif response.status_code in {401, 403, 429}:
                logger.warning("http_request_rejected", outcome="rejected", **log_kwargs)
            elif response.status_code >= 400:
                logger.info("http_request_client_error", outcome="client_error", **log_kwargs)
            else:
                logger.debug("http_request", outcome="success", **log_kwargs)

            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


def _database_unavailable_response(request: Request) -> Response:
    headers = {"Cache-Control": "no-store"}
    read = request.method in {"GET", "HEAD"}
    if read:
        headers["Retry-After"] = "5"
    message = (
        "Pullbox cannot reach the library database. Try again when it is available."
        if read
        else "Pullbox cannot reach the library database. Check the status of your last action "
        "before trying it again."
    )
    if (
        "text/html" in request.headers.get("accept", "").lower()
        and not request.url.path.startswith(("/api/", "/htmx/"))
        and request.headers.get("hx-request", "").lower() != "true"
    ):
        from pullbox.ui.routes import templates

        return templates.TemplateResponse(
            request,
            "pages/database_unavailable.html",
            {"message": message, "can_reload": read},
            status_code=503,
            headers=headers,
        )
    return JSONResponse(
        {"error": {"code": "DATABASE_UNAVAILABLE", "message": message}},
        status_code=503,
        headers=headers,
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Global API rate limiting middleware.

    Enforces per-IP request limits across three tiers based on endpoint
    cost. Returns 429 with Retry-After header when limits are exceeded.
    Adds X-RateLimit-Limit and X-RateLimit-Remaining headers to all
    responses for well-behaved clients.
    """

    def __init__(self, app: object, *, limiter: APIRateLimiter) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limiter = limiter

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._limiter.enabled:
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        path = request.url.path
        method = request.method

        try:
            await self._limiter.check(ip, path, method, headers=request.headers)
        except Exception as exc:
            from pullbox.core.exceptions import PullboxError

            if isinstance(exc, PullboxError) and exc.status_code == 429:
                # Best-effort audit log for API rate limiting
                try:
                    from pullbox.models.audit_log import AuditEventType
                    from pullbox.services.audit_service import audit_event

                    await audit_event(
                        AuditEventType.API_RATE_LIMITED,
                        source_ip=ip,
                        detail=f"API rate limit exceeded: {method} {path}",
                    )
                except Exception:
                    pass
                return JSONResponse(
                    status_code=429,
                    content={"error": {"code": "RATE_LIMITED", "message": exc.message}},
                    headers={"Retry-After": "60"},
                )
            raise

        response = await call_next(request)

        # Add rate limit headers to successful responses
        remaining = self._limiter.get_remaining(ip, path, method, headers=request.headers)
        limit = self._limiter.get_limit(path, method, headers=request.headers)
        if limit > 0:
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response


# Paths exempt from CSRF enforcement
_CSRF_EXEMPT_PATHS = frozenset(
    {
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/system/setup",
        "/login",
        "/logout",
        "/ping",
        "/health",
    }
)

# Methods that don't change state — no CSRF check needed
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_FETCH_METADATA_BLOCKED_SITES = frozenset({"cross-site"})


def _is_cross_site_browser_request(request: Request) -> bool:
    """Return true when modern browser fetch metadata marks a request cross-site."""
    sec_fetch_site = request.headers.get("sec-fetch-site")
    return sec_fetch_site is not None and sec_fetch_site.lower() in _FETCH_METADATA_BLOCKED_SITES


class CsrfMiddleware(BaseHTTPMiddleware):
    """Enforce CSRF token validation on state-changing requests.

    Compares the X-CSRF-Token header against the CSRF token embedded in
    the session cookie.  Skips validation for safe methods (GET/HEAD/OPTIONS),
    exempt paths (login, setup), API-key authenticated requests, and
    requests with no session cookie.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method.upper()

        # Safe methods don't need CSRF
        if method in _CSRF_SAFE_METHODS:
            return await call_next(request)

        # Exempt paths
        path = request.url.path
        if path in _CSRF_EXEMPT_PATHS:
            if _is_cross_site_browser_request(request):
                logger.warning("csrf_cross_site_exempt_path_blocked", path=path, method=method)
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "CSRF_ERROR",
                            "message": "Cross-site state-changing request blocked.",
                        }
                    },
                )
            return await call_next(request)

        # Check for session cookie
        session_token = request.cookies.get(SESSION_COOKIE_NAME)

        # API-key-only requests are CSRF-proof. If a session cookie is also
        # present, keep enforcing the session CSRF contract rather than letting
        # an arbitrary header downgrade browser-session protection.
        if request.headers.get("x-api-key") and not session_token:
            return await call_next(request)

        if not session_token:
            factory = _request_session_factory(request)
            settings = get_settings()
            async with factory() as session:
                bypass_resolution = await resolve_local_auth_bypass(
                    request,
                    session,
                    settings.trusted_proxies,
                )
            if not isinstance(bypass_resolution, LocalAuthBypassResolution):
                bypass_resolution = None
            if bypass_resolution.user_id is None or not bypass_resolution.csrf_token:
                # No trusted bypass = auth layer will handle the 401
                return await call_next(request)

            request.state.local_bypass_resolution = bypass_resolution
            submitted = request.headers.get("x-csrf-token")
            if not submitted:
                logger.warning("csrf_token_missing", path=path, method=method)
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "CSRF_ERROR", "message": "CSRF token missing."}},
                )

            if not secrets.compare_digest(bypass_resolution.csrf_token, submitted):
                logger.warning("csrf_token_invalid", path=path, method=method)
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "CSRF_ERROR", "message": "CSRF token invalid."}},
                )

            return await call_next(request)

        # Extract expected CSRF from session token
        expected = AuthService.get_csrf_token_from_session(session_token)
        if not expected:
            # Invalid/expired session — auth layer will handle it
            return await call_next(request)

        # Get submitted CSRF from header
        submitted = request.headers.get("x-csrf-token")
        if not submitted:
            logger.warning("csrf_token_missing", path=path, method=method)
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "CSRF_ERROR", "message": "CSRF token missing."}},
            )

        if not secrets.compare_digest(expected, submitted):
            logger.warning("csrf_token_invalid", path=path, method=method)
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "CSRF_ERROR", "message": "CSRF token invalid."}},
            )

        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every HTTP response.

    Prevents MIME-sniffing, clickjacking, and information leakage via
    referrer. Applies a Content-Security-Policy that allows ComicVine
    cover images. HSTS is only added in production mode.
    """

    def __init__(self, app: object, *, debug: bool = False) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.debug = debug

    # Paths that serve third-party CDN assets (Swagger UI, ReDoc) and need
    # a relaxed CSP so their scripts/styles can load.
    _DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        path = request.url.path
        if path in self._DOCS_PATHS:
            # Allow Swagger UI / ReDoc CDN assets to load
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://fastapi.tiangolo.com; "
                "connect-src 'self'; "
                "font-src 'self'; "
                "frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: "
                "https://comicvine.gamespot.com https://*.gamespot.com "
                "https://s3.amazonaws.com https://metron.cloud/media/ "
                "https://static.metron.cloud/media/; "
                "connect-src 'self'; "
                "font-src 'self'; "
                "frame-ancestors 'none'"
            )

        if not self.debug:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # Suppress server identification header to avoid information disclosure.
        if "server" in response.headers:
            del response.headers["server"]

        path = request.url.path
        is_cover_path = (
            path.startswith("/api/v1/series/") or path.startswith("/api/v1/issues/")
        ) and path.endswith("/cover")
        is_reader_page_path = path.startswith("/api/v1/reader/issues/") and "/pages/" in path
        # Prevent browser from caching authenticated HTML pages so the back
        # button cannot display stale content after logout. Cover endpoints
        # are exempt so the browser can reuse already-fetched artwork.
        if (
            not path.startswith(("/static/", "/favicon.ico"))
            and not is_cover_path
            and not is_reader_page_path
        ):
            response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response
