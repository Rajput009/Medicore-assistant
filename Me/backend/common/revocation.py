"""Access-token revocation (denylist).

JWTs are otherwise irrevocable until ``exp``. A stolen token must be killable
before then (logout, compromised account, role change). We store a denylist
keyed by the token's ``jti`` (JWT ID) until the token's natural expiry — after
that the signature check alone rejects it, so the entry can evaporate.

Storage preference:
  1. Redis (shared across replicas) when available
  2. In-process set (single-replica / tests / Redis down)

The in-process fallback is weaker under horizontal scale: a token revoked on
pod A is still accepted by pod B until it expires. Production should run Redis.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from backend.common.redis_client import get_redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "medicore:revoked:"

# In-process denylist: jti -> expires_at (unix seconds)
_local: dict[str, float] = {}
_local_lock = threading.Lock()
_last_sweep = 0.0


def reset_revocation_store() -> None:
    """Clear the in-process denylist (tests)."""
    global _last_sweep
    with _local_lock:
        _local.clear()
        _last_sweep = 0.0


def _sweep_local(now: float) -> None:
    global _last_sweep
    if now - _last_sweep < 30:
        return
    expired = [jti for jti, exp in _local.items() if exp <= now]
    for jti in expired:
        _local.pop(jti, None)
    _last_sweep = now


def revoke(jti: str, expires_at: int | float) -> None:
    """Mark ``jti`` as revoked until ``expires_at`` (unix seconds)."""
    if not jti:
        return
    now = time.time()
    ttl = max(1, int(expires_at - now))
    client = get_redis()
    if client is not None:
        try:
            client.setex(f"{_KEY_PREFIX}{jti}", ttl, "1")
            return
        except Exception:
            logger.warning("redis revoke failed; using local store", exc_info=True)

    with _local_lock:
        _sweep_local(now)
        _local[jti] = float(expires_at)


def is_revoked(jti: str | None) -> bool:
    """Return True when ``jti`` is on the denylist."""
    if not jti:
        return False
    client = get_redis()
    if client is not None:
        try:
            return bool(client.exists(f"{_KEY_PREFIX}{jti}"))
        except Exception:
            logger.warning("redis revoke-check failed; using local store", exc_info=True)

    now = time.time()
    with _local_lock:
        _sweep_local(now)
        exp = _local.get(jti)
        if exp is None:
            return False
        if exp <= now:
            _local.pop(jti, None)
            return False
        return True


def revoke_payload(payload: dict[str, Any]) -> bool:
    """Revoke from verified claims. Returns False if the token has no jti."""
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        return False
    revoke(str(jti), float(exp))
    return True
