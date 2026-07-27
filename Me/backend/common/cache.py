"""Postgres-backed cache for FHIR search responses."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import asyncpg

from .config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.pool.Pool | None = None
_pool_lock: asyncio.Lock | None = None
_janitor_task: asyncio.Task | None = None


def _get_lock() -> asyncio.Lock:
    global _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    return _pool_lock


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Decode jsonb/json columns into Python objects automatically."""
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
            format="text",
        )


async def init_pool() -> asyncpg.pool.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _get_lock():
        # Another coroutine may have created the pool while we waited.
        if _pool is not None:
            return _pool
        pool = await asyncpg.create_pool(
            dsn=settings.sqlalchemy_dsn,
            min_size=settings.postgres_min_pool_size,
            max_size=settings.postgres_max_pool_size,
            command_timeout=settings.postgres_command_timeout_seconds,
            init=_init_connection,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fhir_cache (
                    key TEXT PRIMARY KEY,
                    resource TEXT NOT NULL,
                    params JSONB,
                    response JSONB,
                    fetched_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
                );
                """
            )
            # Invalidation queries filter on these; without indexes they are
            # full table scans.
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS fhir_cache_resource_idx "
                "ON fhir_cache (resource);"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS fhir_cache_fetched_at_idx "
                "ON fhir_cache (fetched_at);"
            )
        _pool = pool
        return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _make_key(resource: str, params: dict[str, Any] | None) -> str:
    """Deterministic cache key from resource + sorted params."""
    if not params:
        return f"{resource}::"
    items = sorted((str(k), str(v)) for k, v in params.items())
    return f"{resource}::" + "&".join(f"{k}={v}" for k, v in items)


async def get_cached(
    resource: str,
    params: dict[str, Any],
    max_age_seconds: int = 300,
) -> dict[str, Any] | None:
    pool = await init_pool()
    key = _make_key(resource, params)
    async with pool.acquire() as conn:
        # Compare timestamps in the database so the app server's clock/timezone
        # cannot cause entries to be treated as fresh forever (or never).
        row = await conn.fetchrow(
            """
            SELECT response
            FROM fhir_cache
            WHERE key = $1
              AND fetched_at > now() - ($2::double precision * interval '1 second')
            """,
            key,
            float(max_age_seconds),
        )
    if not row:
        return None
    return row["response"]


async def set_cached(
    resource: str,
    params: dict[str, Any],
    response: dict[str, Any],
) -> None:
    pool = await init_pool()
    key = _make_key(resource, params)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO fhir_cache(key, resource, params, response, fetched_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (key) DO UPDATE
                SET response = EXCLUDED.response,
                    params = EXCLUDED.params,
                    resource = EXCLUDED.resource,
                    fetched_at = EXCLUDED.fetched_at;
            """,
            key,
            resource,
            params or {},
            response,
        )


async def invalidate_cache(resource: str, patient_id: str | None = None) -> int:
    """Delete cache entries for ``resource``; returns the number of rows removed."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        if patient_id:
            status = await conn.execute(
                "DELETE FROM fhir_cache WHERE resource = $1 AND params->>'patient' = $2",
                resource,
                patient_id,
            )
        else:
            status = await conn.execute(
                "DELETE FROM fhir_cache WHERE resource = $1",
                resource,
            )
    # asyncpg returns e.g. "DELETE 3"
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def ping() -> None:
    """Raise if the cache database is not reachable (used by readiness)."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        await conn.execute("SELECT 1")


async def purge_expired(max_age_seconds: int | None = None) -> int:
    """Delete cache rows older than the retention window.

    Without this the table grows without bound: entries are only ever
    overwritten on an identical key, so every distinct search accumulates a row
    forever. That is both a disk-exhaustion risk and, because the rows contain
    PHI, a data-retention problem.
    """
    ttl = max_age_seconds if max_age_seconds is not None else settings.cache_max_age_seconds
    pool = await init_pool()
    async with pool.acquire() as conn:
        status_line = await conn.execute(
            "DELETE FROM fhir_cache "
            "WHERE fetched_at < now() - ($1::double precision * interval '1 second')",
            float(ttl),
        )
    try:
        return int(status_line.split()[-1])
    except (ValueError, IndexError):
        return 0


async def _janitor_loop(interval: int) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            removed = await purge_expired()
            if removed:
                logger.info("purged %d expired cache rows", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed sweep must never kill the loop.
            logger.warning("cache purge failed", exc_info=True)


async def start_janitor() -> None:
    """Start the periodic expiry sweep (idempotent)."""
    global _janitor_task
    if _janitor_task is not None and not _janitor_task.done():
        return
    interval = settings.cache_cleanup_interval_seconds
    if interval <= 0:
        return
    _janitor_task = asyncio.create_task(_janitor_loop(interval), name="fhir-cache-janitor")


async def stop_janitor() -> None:
    global _janitor_task
    if _janitor_task is None:
        return
    _janitor_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _janitor_task
    _janitor_task = None
