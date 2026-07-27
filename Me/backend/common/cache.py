"""Postgres-backed cache for FHIR search responses."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg

from .config import settings

_pool: asyncpg.pool.Pool | None = None
_pool_lock: asyncio.Lock | None = None


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
            min_size=1,
            max_size=5,
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
