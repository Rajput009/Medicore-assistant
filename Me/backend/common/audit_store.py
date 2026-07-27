"""Queryable audit index (PostgreSQL).

HIPAA 164.312(b) requires being able to answer "who accessed this patient's
record?". The audit *stream* on stdout already carries that information, but a
log stream cannot be queried by a compliance officer during an investigation.
This module maintains a searchable index of the same events.

Two design points matter:

**The log remains the system of record.** This table is an index over it. Writes
are buffered and best-effort: a database outage degrades search, it must never
fail a clinical request or slow one down. Anything dropped here is still on
stdout, and the drop is counted and logged so the gap is visible rather than
silent.

**Patient references stay pseudonymised.** The middleware writes a salted HMAC,
not an MRN. Search works anyway because the query path hashes its input with the
same salt before matching — so "who viewed MRN-X?" is answerable without the
index itself becoming a PHI store. Sites that set
``AUDIT_LOG_RAW_IDENTIFIERS=true`` store raw ids and searching still works,
because the same transform is applied on both sides.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any

import asyncpg

from .config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.pool.Pool | None = None
_pool_lock: asyncio.Lock | None = None
_queue: asyncio.Queue[dict[str, Any]] | None = None
_writer_task: asyncio.Task | None = None

# Counts events the buffer could not accept. Surfaced on /ready so a silent
# audit gap becomes an operational signal instead of a surprise at audit time.
_dropped = 0

# Bounded so a database stall cannot grow the buffer until the pod is OOM-killed.
_MAX_QUEUE = 2000
# Rows per write. Batching keeps a busy gateway from issuing one INSERT per
# request while still flushing promptly.
_BATCH = 50

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    request_id TEXT,
    service TEXT,
    method TEXT,
    path TEXT,
    status INTEGER,
    outcome TEXT,
    actor_sub TEXT,
    actor_roles JSONB,
    resource_type TEXT,
    resource_ref TEXT,
    patient_ref TEXT,
    client_ip TEXT,
    duration_ms DOUBLE PRECISION
);
"""

# "Who touched this patient", "what did this user do" and any time-bounded
# report are the three questions actually asked; without these they are scans.
CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS audit_events_patient_idx "
    "ON audit_events (patient_ref, occurred_at DESC);",
    "CREATE INDEX IF NOT EXISTS audit_events_actor_idx "
    "ON audit_events (actor_sub, occurred_at DESC);",
    "CREATE INDEX IF NOT EXISTS audit_events_occurred_idx "
    "ON audit_events (occurred_at DESC);",
)

_COLUMNS = (
    "occurred_at", "request_id", "service", "method", "path", "status",
    "outcome", "actor_sub", "actor_roles", "resource_type", "resource_ref",
    "patient_ref", "client_ip", "duration_ms",
)


def _get_lock() -> asyncio.Lock:
    global _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    return _pool_lock


async def _init_connection(conn: asyncpg.Connection) -> None:
    import json

    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
            format="text",
        )


async def init_pool() -> asyncpg.pool.Pool:
    """Create the pool and ensure the schema exists."""
    global _pool
    if _pool is not None:
        return _pool
    async with _get_lock():
        if _pool is not None:
            return _pool
        pool = await asyncpg.create_pool(
            dsn=settings.sqlalchemy_dsn,
            min_size=1,
            max_size=max(2, settings.postgres_max_pool_size // 2),
            command_timeout=settings.postgres_command_timeout_seconds,
            init=_init_connection,
        )
        async with pool.acquire() as conn:
            await conn.execute(CREATE_TABLE)
            for statement in CREATE_INDEXES:
                await conn.execute(statement)
        _pool = pool
        return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def dropped_events() -> int:
    return _dropped


def reset_dropped() -> None:
    global _dropped
    _dropped = 0


def enqueue(event: dict[str, Any]) -> bool:
    """Hand an event to the writer without blocking the request.

    Returns False when the buffer is full. The caller does not retry: the event
    is already on stdout, and stalling a clinical request to record an audit
    row would trade patient-facing latency for archival completeness.
    """
    global _dropped
    if _queue is None:
        return False
    try:
        _queue.put_nowait(event)
        return True
    except asyncio.QueueFull:
        _dropped += 1
        # One line per drop would itself flood the log during an outage.
        if _dropped % 100 == 1:
            logger.warning(
                "audit index buffer full; events are on stdout only",
                extra={"dropped_total": _dropped},
            )
        return False


async def _insert_batch(rows: list[dict[str, Any]]) -> None:
    """Insert a batch in one round trip.

    Uses executemany rather than COPY: asyncpg's COPY path requires *binary*
    encoders, and the jsonb codec registered here (shared with the FHIR cache
    for consistency) is text-format, so COPY rejects the roles column outright.
    executemany still batches over a single connection, which is what the
    round-trip cost is actually about at this volume.
    """
    pool = await init_pool()
    placeholders = ", ".join(f"${i + 1}" for i in range(len(_COLUMNS)))
    statement = (
        f"INSERT INTO audit_events ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
    )
    records = [tuple(row.get(column) for column in _COLUMNS) for row in rows]
    async with pool.acquire() as conn:
        await conn.executemany(statement, records)


async def _writer_loop() -> None:
    """Drain the buffer into Postgres in batches until cancelled."""
    assert _queue is not None
    while True:
        try:
            first = await _queue.get()
            batch = [first]
            # Opportunistically take whatever else is already waiting.
            while len(batch) < _BATCH:
                try:
                    batch.append(_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await _insert_batch(batch)
            except Exception:
                # Losing the batch is acceptable; losing the writer is not.
                logger.warning("audit index write failed", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.warning("audit writer iteration failed", exc_info=True)


async def start_writer() -> None:
    """Start buffered indexing (idempotent)."""
    global _queue, _writer_task
    if _writer_task is not None and not _writer_task.done():
        return
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_MAX_QUEUE)
    await init_pool()
    _writer_task = asyncio.create_task(_writer_loop(), name="audit-index-writer")


async def stop_writer(drain: bool = True) -> None:
    """Stop indexing, flushing what is already buffered."""
    global _writer_task, _queue
    if _writer_task is None:
        return
    if drain and _queue is not None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_queue.join(), timeout=2.0)
    _writer_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _writer_task
    _writer_task = None
    _queue = None


async def flush_pending() -> None:
    """Write everything buffered right now. Used by tests and shutdown."""
    if _queue is None:
        return
    batch: list[dict[str, Any]] = []
    while True:
        try:
            batch.append(_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    if batch:
        await _insert_batch(batch)


async def record_now(event: dict[str, Any]) -> None:
    """Write a single event synchronously (used where losing it is not ok)."""
    await _insert_batch([event])


async def search(
    *,
    patient_ref: str | None = None,
    actor_sub: str | None = None,
    resource_type: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total_matching).

    ``patient_ref`` must already be in stored form — callers pass the output of
    the same pseudonymisation the middleware applied, never a raw MRN.
    """
    pool = await init_pool()

    clauses: list[str] = []
    args: list[Any] = []

    def add(clause_template: str, value: Any) -> None:
        args.append(value)
        clauses.append(clause_template.format(n=len(args)))

    if patient_ref:
        # A patient's record may be touched via either reference column.
        add("(patient_ref = ${n} OR resource_ref = ${n})", patient_ref)
    if actor_sub:
        add("actor_sub = ${n}", actor_sub)
    if resource_type:
        add("resource_type = ${n}", resource_type)
    if outcome:
        add("outcome = ${n}", outcome)
    if since:
        add("occurred_at >= ${n}", since)
    if until:
        add("occurred_at <= ${n}", until)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM audit_events {where}", *args
        )
        rows = await conn.fetch(
            f"""
            SELECT occurred_at, request_id, service, method, path, status,
                   outcome, actor_sub, actor_roles, resource_type,
                   resource_ref, patient_ref, client_ip, duration_ms
            FROM audit_events
            {where}
            ORDER BY occurred_at DESC, id DESC
            LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
            """,
            *args,
            limit,
            offset,
        )

    return [dict(row) for row in rows], int(total or 0)


async def purge_older_than(days: int) -> int:
    """Delete audit rows past the retention window.

    Deliberately not wired to the FHIR cache janitor: cached PHI should expire
    in hours, whereas an access log is typically retained for years (HIPAA
    164.316(b)(2) says six). Sharing a sweep would quietly shred the trail.
    """
    pool = await init_pool()
    async with pool.acquire() as conn:
        status_line = await conn.execute(
            "DELETE FROM audit_events "
            "WHERE occurred_at < now() - ($1::double precision * interval '1 day')",
            float(days),
        )
    try:
        return int(status_line.split()[-1])
    except (ValueError, IndexError):
        return 0


async def ping() -> None:
    pool = await init_pool()
    async with pool.acquire() as conn:
        await conn.execute("SELECT 1")
