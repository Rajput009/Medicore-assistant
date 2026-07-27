"""Optional Redis client shared by rate limiting and token revocation.

Redis is best-effort infrastructure: if it is unreachable the callers fall back
to in-process behaviour so a cache outage cannot take the API down. Production
deployments should run Redis and monitor the fallback metric.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.common.config import settings

logger = logging.getLogger(__name__)

_client: Any | None = None
_client_failed = False


def reset_redis_client() -> None:
    """Drop the memoised client (tests / reconnect after config change)."""
    global _client, _client_failed
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None
    _client_failed = False


def get_redis() -> Any | None:
    """Return a connected Redis client, or ``None`` when unavailable.

    Import of ``redis`` is deferred so unit tests that never touch Redis do not
    require the package. Connection failures are cached for the process lifetime
    of the failure window so we do not hammer a down host on every request.
    """
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        return None
    if not settings.redis_enabled:
        return None
    try:
        import redis  # type: ignore
    except ImportError:
        _client_failed = True
        return None

    try:
        client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password or None,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
            decode_responses=True,
            health_check_interval=30,
        )
        client.ping()
        _client = client
        return _client
    except Exception as exc:
        logger.warning(
            "redis unavailable; falling back to in-process stores",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:200]},
        )
        _client_failed = True
        return None
