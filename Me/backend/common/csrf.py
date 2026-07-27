"""CSRF defence for cookie-authenticated requests.

Bearer tokens in the ``Authorization`` header are not sent automatically by
the browser on cross-site requests, so they are not a CSRF vector. An
httpOnly session cookie **is** sent automatically, so a hostile page could
trigger state-changing calls while a clinician is signed in.

Defence (defence-in-depth, applied together):

1. ``SameSite=Lax`` on the session cookie (set at issue time) blocks most
   cross-site POSTs already.
2. This middleware rejects unsafe methods authenticated **only** by cookie
   unless ``Origin`` (preferred) or ``Referer`` matches the configured
   ``ALLOWED_ORIGINS`` allow-list.
3. A double-submit CSRF header (``X-CSRF-Token`` matching the
   ``medicore_csrf`` cookie) is also accepted, so non-browser clients that
   cannot set Origin still work when they opt in.

Safe methods (GET/HEAD/OPTIONS) are never blocked: they must not mutate
state, and SameSite=Lax already sends the cookie on top-level navigations.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from backend.common.config import settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-csrf-token"
CSRF_COOKIE = "medicore_csrf"


def _origin_allowed(origin: str, allowed: Iterable[str]) -> bool:
    if not origin:
        return False
    origin = origin.rstrip("/")
    for candidate in allowed:
        cand = candidate.rstrip("/")
        if not cand:
            continue
        if cand == "*":
            # Wildcard is rejected at boot in production; treat as deny here
            # so a misconfiguration never becomes an open CSRF door.
            return False
        if origin == cand:
            return True
    return False


def _origin_from_referer(referer: str) -> str | None:
    if not referer:
        return None
    try:
        parsed = urlparse(referer)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def request_has_bearer(request: Request) -> bool:
    auth = request.headers.get("authorization") or ""
    scheme, _, token = auth.partition(" ")
    return scheme.lower() == "bearer" and bool(token.strip())


def request_has_session_cookie(request: Request) -> bool:
    cookie = request.cookies.get(settings.auth_cookie_name)
    return bool(cookie and cookie.strip())


def csrf_header_matches_cookie(request: Request) -> bool:
    header = (request.headers.get(CSRF_HEADER) or "").strip()
    cookie = (request.cookies.get(CSRF_COOKIE) or "").strip()
    if not header or not cookie:
        return False
    # secrets.compare_digest requires equal-length strings; mismatched lengths
    # are simply not equal.
    if len(header) != len(cookie):
        return False
    return secrets.compare_digest(header, cookie)


class CookieCSRFMiddleware(BaseHTTPMiddleware):
    """Block cookie-only unsafe requests that fail the Origin/CSRF check."""

    def __init__(
        self,
        app,
        *,
        exempt_paths: Iterable[str] = ("/health", "/ready", "/metrics"),
    ):
        super().__init__(app)
        self.exempt_paths = frozenset(p.rstrip("/") or "/" for p in exempt_paths)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in UNSAFE_METHODS:
            return await call_next(request)

        path = request.url.path.rstrip("/") or "/"
        if path in self.exempt_paths:
            return await call_next(request)

        # Bearer auth is not a CSRF vector.
        if request_has_bearer(request):
            return await call_next(request)

        # No session cookie either: the auth layer will 401. Do not CSRF-block
        # anonymous calls (e.g. /login) — that would break sign-in itself.
        if not request_has_session_cookie(request):
            return await call_next(request)

        # Double-submit CSRF token satisfies the check without Origin.
        if csrf_header_matches_cookie(request):
            return await call_next(request)

        allowed = settings.cors_origins
        origin = (request.headers.get("origin") or "").strip()
        if origin and _origin_allowed(origin, allowed):
            return await call_next(request)

        referer_origin = _origin_from_referer(request.headers.get("referer") or "")
        if referer_origin and _origin_allowed(referer_origin, allowed):
            return await call_next(request)

        return JSONResponse(
            {
                "detail": (
                    "CSRF check failed: cookie-authenticated mutations require "
                    "a matching Origin/Referer or X-CSRF-Token header"
                )
            },
            status_code=403,
        )


def issue_csrf_cookie(response: Response, *, secure: bool) -> str:
    """Set a fresh double-submit CSRF cookie and return its value.

    The SPA reads this non-httpOnly cookie and echoes it as ``X-CSRF-Token``
    on unsafe requests. The session JWT stays httpOnly.
    """
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        max_age=settings.access_token_ttl_minutes * 60,
        httponly=False,  # must be readable by JS to echo as a header
        secure=secure,
        samesite="lax",
        path="/",
    )
    return token


def clear_csrf_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        key=CSRF_COOKIE,
        path="/",
        httponly=False,
        secure=secure,
        samesite="lax",
    )
