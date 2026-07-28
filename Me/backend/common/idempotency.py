"""Idempotency-Key support for unsafe writes.

Clients may retry POST/PATCH after a network blip. Without an idempotency key
a double-submit can enqueue the same patient twice (when the first write
committed but the response was lost) or apply conflicting bed updates.

We store a short-lived record of ``(principal, route, key) → response`` in
Redis when available, else in-process (single-replica / tests).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse, Response

from backend.common.redis_client import get_redis

logger = logging.getLogger(__name__)

_HEADER = "idempotency-key"
_KEY_PREFIX = "medicore:idem:"
_TTL_SECONDS = 24 * 3600
_MAX_KEY_LEN = 128

_local: dict[str, tuple[float, int, str]] = {}
_local_lock = threading.Lock()


def reset_idempotency_store() -> None:
    with _local_lock:
        _local.clear()


def _composite_key(principal: str, route: str, key: str) -> str:
    raw = f"{principal}|{route}|{key}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:40]
    return f"{_KEY_PREFIX}{digest}"


def extract_idempotency_key(request: Request) -> str | None:
    value = (request.headers.get(_HEADER) or "").strip()
    if not value:
        return None
    if len(value) > _MAX_KEY_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Idempotency-Key must be at most {_MAX_KEY_LEN} characters",
        )
    # Printable ASCII only — avoid header injection / log noise.
    if any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key contains invalid characters",
        )
    return value


def lookup(principal: str, route: str, key: str) -> tuple[int, Any] | None:
    """Return ``(status_code, body)`` if this key was already completed."""
    ck = _composite_key(principal, route, key)
    client = get_redis()
    if client is not None:
        try:
            raw = client.get(ck)
            if raw:
                data = json.loads(raw)
                return int(data["status"]), data["body"]
        except Exception:
            logger.warning("idempotency redis lookup failed", exc_info=True)

    now = time.time()
    with _local_lock:
        hit = _local.get(ck)
        if not hit:
            return None
        exp, status_code, payload_json = hit
        if exp <= now:
            _local.pop(ck, None)
            return None
        # ``store`` writes the same {"status", "body"} envelope on both paths,
        # so unwrap it here too. Returning the envelope made an in-process
        # replay answer {"status": 200, "body": {...}} instead of the original
        # body — a silently different response shape whenever Redis was off.
        return status_code, json.loads(payload_json)["body"]


def store(principal: str, route: str, key: str, status_code: int, body: Any) -> None:
    ck = _composite_key(principal, route, key)
    # Encode exactly as FastAPI would, so a replay is byte-for-byte the
    # response the client missed. Plain ``default=str`` renders datetimes as
    # "2026-07-27 20:11:10+00:00" while FastAPI emits ISO-8601 with a "T" —
    # a retry would then hand the caller a different timestamp format than
    # the original, which any client parsing dates would choke on.
    # Encode the way the route itself does. These handlers return plain dicts
    # (no response_model), so FastAPI serialises them with jsonable_encoder;
    # matching it here keeps a replay byte-identical to the response the
    # client missed. A route that later adopts a response_model would be
    # serialised by pydantic instead ("...Z" rather than "...+00:00"), so
    # that change must come with a matching change here.
    ck_body = jsonable_encoder(body)
    payload = json.dumps({"status": status_code, "body": ck_body}, default=str)
    client = get_redis()
    if client is not None:
        try:
            client.setex(ck, _TTL_SECONDS, payload)
            return
        except Exception:
            logger.warning("idempotency redis store failed", exc_info=True)

    with _local_lock:
        _local[ck] = (time.time() + _TTL_SECONDS, status_code, payload)


def replay_response(status_code: int, body: Any) -> Response:
    return JSONResponse(content=body, status_code=status_code, headers={"Idempotent-Replayed": "true"})
