"""Transport and abuse-protection middleware.

These are edge concerns that a reverse proxy may also handle, but relying on
that is a single point of failure: if the proxy is bypassed (port-forward, a
misrouted internal call, a mesh sidecar in permissive mode) the application
must still defend itself.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Static headers applied to every response.
SECURITY_HEADERS: dict[str, str] = {
    # Stop browsers guessing content types (XSS vector for JSON endpoints).
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # PHI must never be written to a shared or disk cache.
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    # API responses never need to execute anything.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds hardening headers, including HSTS when served over TLS."""

    def __init__(self, app, hsts: bool = True, hsts_max_age: int = 31_536_000):
        super().__init__(app)
        self.hsts = hsts
        self.hsts_max_age = hsts_max_age

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # Only advertise HSTS on requests that arrived over TLS, otherwise a
        # local HTTP dev setup becomes unreachable after one visit.
        forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        if self.hsts and forwarded_proto == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={self.hsts_max_age}; includeSubDomains",
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies before they are buffered."""

    def __init__(self, app, max_bytes: int = 1_048_576):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_bytes:
                    return JSONResponse(
                        {"detail": "Request body too large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"detail": "Invalid Content-Length header"}, status_code=400
                )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window-per-caller rate limiting.

    Prefers Redis when available so N replicas share one global budget. Falls
    back to an in-process deque when Redis is down or disabled — that weakens
    the ceiling to N x limit, which is still enough to blunt credential
    stuffing, and is the only option in unit tests without a Redis broker.
    """

    def __init__(
        self,
        app,
        limit: int = 120,
        window_seconds: int = 60,
        exempt_paths: tuple[str, ...] = ("/health", "/ready", "/metrics"),
        key_func: Callable[[Request], str] | None = None,
        redis_prefix: str = "medicore:rl:",
    ):
        super().__init__(app)
        self.limit = limit
        self.window = window_seconds
        self.exempt_paths = exempt_paths
        self.key_func = key_func or self._default_key
        self.redis_prefix = redis_prefix
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    @staticmethod
    def _default_key(request: Request) -> str:
        # Prefer the authenticated subject so one abusive user cannot exhaust
        # the budget for everyone behind the same NAT/proxy address.
        user = getattr(request.state, "user", None)
        if isinstance(user, dict) and user.get("sub"):
            return f"sub:{user['sub']}"
        # Cookie-authenticated sessions (auth service) expose principal the
        # same way once JWT middleware has run; fall back to IP otherwise.
        principal = getattr(request.state, "principal", None)
        if principal is not None and getattr(principal, "sub", None):
            return f"sub:{principal.sub}"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
        return f"ip:{request.client.host if request.client else 'unknown'}"

    def _sweep(self, now: float) -> None:
        """Drop idle buckets so the map cannot grow without bound."""
        if now - self._last_sweep < self.window:
            return
        cutoff = now - self.window
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            self._hits.pop(key, None)
        self._last_sweep = now

    def _hit_redis(self, key: str) -> tuple[bool, int, int] | None:
        """Return (allowed, remaining, retry_after) via Redis, or None on miss."""
        try:
            from backend.common.redis_client import get_redis
        except Exception:
            return None
        client = get_redis()
        if client is None:
            return None
        redis_key = f"{self.redis_prefix}{key}"
        try:
            # Atomic INCR + EXPIRE on first hit. Fixed window starting at first
            # request in the window — simpler and good enough for abuse blunt.
            pipe = client.pipeline()
            pipe.incr(redis_key)
            pipe.ttl(redis_key)
            count, ttl = pipe.execute()
            if ttl == -1 or (count == 1 and ttl < 0):
                client.expire(redis_key, self.window)
                ttl = self.window
            if count > self.limit:
                return False, 0, max(1, int(ttl) if ttl and ttl > 0 else self.window)
            return True, max(0, self.limit - int(count)), max(1, int(ttl) if ttl and ttl > 0 else self.window)
        except Exception:
            return None

    def _hit_local(self, key: str) -> tuple[bool, int, int]:
        now = time.monotonic()
        self._sweep(now)
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            retry_after = max(1, int(self.window - (now - bucket[0])))
            return False, 0, retry_after
        bucket.append(now)
        return True, max(0, self.limit - len(bucket)), self.window

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        key = self.key_func(request)
        result = self._hit_redis(key)
        if result is None:
            allowed, remaining, retry_after = self._hit_local(key)
        else:
            allowed, remaining, retry_after = result

        if not allowed:
            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers.setdefault("X-RateLimit-Limit", str(self.limit))
        response.headers.setdefault("X-RateLimit-Remaining", str(remaining))
        return response