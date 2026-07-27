"""Queryable audit index (HIPAA 45 CFR 164.312(b) / 164.308(a)(1)(ii)(D)).

``middleware.AuditLogMiddleware`` already emits a structured record for every
request. That satisfies "write it down" but not "answer a question": a log
stream cannot tell a privacy officer *who opened MRN-123 last Tuesday* without
shipping the whole stream somewhere else and querying it there.

This module adds a second, queryable sink in Postgres. Design constraints, in
priority order:

1. **A clinical request must never wait on, or fail because of, an audit
   write.** The sink is a bounded in-memory queue drained by a background
   task. ``submit`` is synchronous, non-blocking and cannot raise.
2. **Dropping an audit record is a compliance event, not a silent detail.**
   When the queue overflows or a batch fails to write, counters increment and
   a rate-limited warning is logged. ``stats()`` exposes them so the condition
   is monitorable rather than invisible. This is a deliberate trade: losing a
   few audit rows is bad, blocking a clinician mid-resuscitation is worse.
3. **The log stream remains the system of record.** This table is an index for
   investigation. It is derived data and may be rebuilt from the logs.

Identifiers stored here are whatever ``middleware.audit_reference`` produced —
pseudonymised by default, so this table carries no raw MRNs unless the
deployment explicitly opted into ``AUDIT_LOG_RAW_IDENTIFIERS``.

Known limitation: a request that reads a non-Patient resource directly (e.g.
``GET /fhir/observation/obs-1``) records that resource's reference, not the
patient it belongs to. Attributing those to a patient needs the upstream
resource's ``subject``, which the gateway does not resolve on the read path.
Searches therefore match a patient's *own* reference plus anything explicitly
filtered by that patient.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

from .cache import init_pool
from .config import settings

logger = logging.getLogger(__name__)

# Probe and static traffic would otherwise dominate the table: a readiness
# probe every 10s across a few replicas is millions of rows a year that answer
# no audit question. They stay in the log stream; they do not get indexed.
SKIPPED_PATHS: frozenset[str] = frozenset(
    {"/health", "/healthz", "/ready", "/metrics", "/favicon.ico"}
)

VALID_OUTCOMES: frozenset[str] = frozenset({"success", "failure", "denied", "error"})

# Defensive column bounds. A hostile client controls the user-agent and can
# influence the path, so neither is trusted to be a sane length.
_MAX_PATH = 512
_MAX_SUB = 256
_MAX_USER_AGENT = 256
_MAX_TEXT = 128

# Hard ceiling on a single page of results, independent of any caller.
MAX_SEARCH_LIMIT = 200

_DDL = """
CREATE TABLE IF NOT EXISTS audit_events (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMP WITH TIME ZONE NOT NULL,
    request_id   TEXT,
    service      TEXT,
    actor_sub    TEXT,
    actor_roles  TEXT[],
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    status       INTEGER,
    outcome      TEXT,
    resource_type TEXT,
    resource_ref TEXT,
    patient_ref  TEXT,
    bed_id       TEXT,
    client_ip    TEXT,
    user_agent   TEXT,
    duration_ms  DOUBLE PRECISION,
    query_keys   TEXT[],
    break_glass  BOOLEAN NOT NULL DEFAULT false,
    break_glass_reason TEXT,
    subject_refs TEXT[],
    subject_count INTEGER
);
"""

# Columns added after the table first shipped. Applied with IF NOT EXISTS so an
# existing deployment upgrades in place rather than needing the audit history
# dropped and recreated.
_MIGRATIONS = (
    "ALTER TABLE audit_events "
    "ADD COLUMN IF NOT EXISTS break_glass BOOLEAN NOT NULL DEFAULT false;",
    "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS break_glass_reason TEXT;",
    "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS subject_refs TEXT[];",
    "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS subject_count INTEGER;",
)

# Every index below backs a question an investigator actually asks. Without
# them "who viewed MRN-X" is a sequential scan over the whole history.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS audit_events_patient_idx "
    "ON audit_events (patient_ref, ts DESC);",
    "CREATE INDEX IF NOT EXISTS audit_events_resource_idx "
    "ON audit_events (resource_ref, ts DESC);",
    "CREATE INDEX IF NOT EXISTS audit_events_actor_idx "
    "ON audit_events (actor_sub, ts DESC);",
    "CREATE INDEX IF NOT EXISTS audit_events_ts_idx ON audit_events (ts DESC);",
    # Denied attempts are the highest-signal rows and are a small fraction of
    # the table, so they get a partial index.
    "CREATE INDEX IF NOT EXISTS audit_events_denied_idx "
    "ON audit_events (ts DESC) WHERE outcome = 'denied';",
    # Break-glass overrides are rarer still and are reviewed as a set
    # ("show me every emergency override this month"), so the same applies.
    "CREATE INDEX IF NOT EXISTS audit_events_break_glass_idx "
    "ON audit_events (ts DESC) WHERE break_glass;",
    # Searches list every patient they disclosed. Without a GIN index,
    # "was MRN-X in anyone's search results?" is a sequential scan.
    "CREATE INDEX IF NOT EXISTS audit_events_subject_refs_idx "
    "ON audit_events USING GIN (subject_refs);",
)

_INSERT = """
INSERT INTO audit_events (
    ts, request_id, service, actor_sub, actor_roles, method, path, status,
    outcome, resource_type, resource_ref, patient_ref, bed_id, client_ip,
    user_agent, duration_ms, query_keys, break_glass, break_glass_reason,
    subject_refs, subject_count
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
    $17, $18, $19, $20, $21
)
"""

_COLUMNS = (
    "ts",
    "request_id",
    "service",
    "actor_sub",
    "actor_roles",
    "method",
    "path",
    "status",
    "outcome",
    "resource_type",
    "resource_ref",
    "patient_ref",
    "bed_id",
    "client_ip",
    "user_agent",
    "duration_ms",
    "query_keys",
    "break_glass",
    "break_glass_reason",
    "subject_refs",
    "subject_count",
)


def _text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


def _string_list(value: Any) -> list[str] | None:
    """Coerce to ``text[]``; asyncpg maps a Python list of str directly."""
    if not value:
        return None
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        return None
    cleaned = [item[:_MAX_TEXT] for item in items if item][:32]
    return cleaned or None


def row_from_record(record: dict[str, Any], *, now: datetime | None = None) -> tuple:
    """Project an audit log record onto the ``audit_events`` column tuple.

    Timestamped at submit time rather than write time so queue latency never
    skews the recorded moment of access.
    """
    status = record.get("status")
    duration = record.get("duration_ms")
    return (
        now or datetime.now(UTC),
        _text(record.get("request_id")),
        _text(record.get("service"), 64),
        _text(record.get("sub"), _MAX_SUB),
        _string_list(record.get("roles")),
        _text(record.get("method"), 16) or "GET",
        _text(record.get("path"), _MAX_PATH) or "/",
        int(status) if isinstance(status, (int, float)) else None,
        _text(record.get("outcome"), 32),
        _text(record.get("resource_type"), 64),
        _text(record.get("resource_ref"), _MAX_SUB),
        _text(record.get("patient_ref"), _MAX_SUB),
        _text(record.get("bed_id"), 64),
        _text(record.get("client_ip"), 64),
        _text(record.get("user_agent"), _MAX_USER_AGENT),
        float(duration) if isinstance(duration, (int, float)) else None,
        _string_list(record.get("query_keys")),
        bool(record.get("break_glass", False)),
        _text(record.get("break_glass_reason"), 500),
        _string_list(record.get("subject_refs")),
        int(record["subject_count"])
        if isinstance(record.get("subject_count"), (int, float))
        else None,
    )


def should_index(record: dict[str, Any]) -> bool:
    """False for traffic that answers no audit question (probes, favicon)."""
    path = str(record.get("path") or "")
    normalised = path.rstrip("/") or "/"
    return normalised not in SKIPPED_PATHS


class AuditWriter:
    """Bounded queue + background batch writer.

    The queue is deliberately bounded. An unbounded one would trade a visible,
    counted drop for an invisible memory leak that eventually OOM-kills the
    pod and loses far more than a handful of records.
    """

    def __init__(
        self,
        *,
        queue_size: int = 5000,
        batch_size: int = 200,
        flush_interval: float = 1.0,
    ) -> None:
        self.queue_size = max(1, queue_size)
        self.batch_size = max(1, batch_size)
        self.flush_interval = max(0.05, flush_interval)
        self._queue: asyncio.Queue[tuple] | None = None
        self._task: asyncio.Task | None = None
        self._closing = False
        # Rows taken off the queue but not yet written. Held on the instance
        # rather than as a local in the collector so that cancelling the task
        # mid-batch (pod shutdown) does not discard them.
        self._pending: list[tuple] = []
        # Counters are the only signal that the index is lossy; they are read
        # by /audit/stats and should be alerted on in production.
        self.written = 0
        self.dropped = 0
        self.failed = 0
        self._last_warn = 0.0

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def depth(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    async def start(self) -> None:
        if self.running:
            return
        self._closing = False
        # Created inside the loop that will drain it.
        self._queue = asyncio.Queue(maxsize=self.queue_size)
        self._task = asyncio.create_task(self._run(), name="audit-index-writer")

    async def stop(self, *, drain_timeout: float = 2.0) -> None:
        """Stop the writer, flushing what is already queued.

        Shutdown is bounded: a pod being drained has a finite grace period, and
        hanging on a wedged database would turn a clean rollout into a kill -9.
        """
        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Flush both the staged mid-batch rows and anything still queued.
        if self._pending or (self._queue is not None and not self._queue.empty()):
            with contextlib.suppress(Exception, TimeoutError):
                await asyncio.wait_for(self._flush_remaining(), timeout=drain_timeout)
        self._queue = None

    # -- ingest ------------------------------------------------------------

    def submit(self, record: dict[str, Any]) -> bool:
        """Queue a record. Never blocks, never raises.

        Returns True when accepted. A False result has already been counted
        and warned about; callers (the audit middleware) must ignore it.
        """
        queue = self._queue
        if queue is None or self._closing:
            return False
        if not should_index(record):
            return False
        try:
            queue.put_nowait(row_from_record(record))
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            self._warn_lossy("audit index queue full; record dropped")
            return False
        except Exception:
            # Malformed record must not propagate into the request path.
            self.dropped += 1
            self._warn_lossy("audit index record could not be encoded")
            return False

    def _warn_lossy(self, message: str) -> None:
        """Warn at most once every 30s — a flood would hide the signal."""
        now = time.monotonic()
        if now - self._last_warn < 30.0:
            return
        self._last_warn = now
        logger.warning(
            message,
            extra={"audit_dropped": self.dropped, "audit_failed": self.failed},
        )

    # -- drain -------------------------------------------------------------

    async def _collect(self) -> list[tuple]:
        """Wait for at least one row, then take whatever else is ready.

        Rows are appended to ``self._pending`` as they are dequeued, so a
        cancellation part-way through a batch leaves them recoverable rather
        than dropping them on the floor.
        """
        assert self._queue is not None
        self._pending.append(await self._queue.get())
        deadline = time.monotonic() + self.flush_interval
        while len(self._pending) < self.batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self._pending.append(
                    await asyncio.wait_for(self._queue.get(), timeout=remaining)
                )
            except TimeoutError:
                break
        batch, self._pending = self._pending, []
        return batch

    async def _run(self) -> None:
        while True:
            try:
                batch = await self._collect()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.warning("audit index collector failed", exc_info=True)
                await asyncio.sleep(self.flush_interval)
                continue
            await self._write(batch)

    async def _flush_remaining(self) -> None:
        """Write anything still buffered: mid-batch rows first, then the queue."""
        batch, self._pending = self._pending, []
        if self._queue is not None:
            while not self._queue.empty() and len(batch) < self.batch_size:
                batch.append(self._queue.get_nowait())
        if batch:
            await self._write(batch)

    async def _write(self, batch: list[tuple]) -> None:
        if not batch:
            return
        try:
            pool = await init_pool()
            async with pool.acquire() as conn:
                # executemany prepares once and reuses the plan. COPY would be
                # faster but uses binary encoding, which conflicts with the
                # text-format json codec the shared pool installs.
                await conn.executemany(_INSERT, batch)
            self.written += len(batch)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One attempt only. Retrying a wedged database just grows the
            # queue until it overflows, converting a counted write failure
            # into a larger counted drop.
            self.failed += len(batch)
            self._warn_lossy("audit index batch write failed")
            logger.debug("audit index write error", exc_info=True)

    def stats(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "queued": self.depth,
            "capacity": self.queue_size,
            "written": self.written,
            "dropped": self.dropped,
            "failed": self.failed,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_writer: AuditWriter | None = None
_janitor: asyncio.Task | None = None


def get_writer() -> AuditWriter | None:
    return _writer


async def ensure_schema() -> None:
    pool = await init_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
        # Bring an already-deployed table up to the current shape before the
        # indexes, which may reference the newer columns.
        for statement in _MIGRATIONS:
            await conn.execute(statement)
        for statement in _INDEXES:
            await conn.execute(statement)


async def start() -> AuditWriter:
    """Create the schema and start the background writer (idempotent)."""
    global _writer
    await ensure_schema()
    if _writer is None:
        _writer = AuditWriter(
            queue_size=settings.audit_index_queue_size,
            batch_size=settings.audit_index_batch_size,
            flush_interval=settings.audit_index_flush_interval_seconds,
        )
    await _writer.start()
    await start_janitor()
    return _writer


async def stop() -> None:
    global _writer
    await stop_janitor()
    if _writer is not None:
        await _writer.stop()
    _writer = None


def submit(record: dict[str, Any]) -> bool:
    """Sink entry point handed to the audit middleware."""
    writer = _writer
    if writer is None:
        return False
    return writer.submit(record)


def stats() -> dict[str, Any]:
    writer = _writer
    if writer is None:
        return {"running": False, "queued": 0, "capacity": 0, "written": 0,
                "dropped": 0, "failed": 0}
    return writer.stats()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def purge_expired(retention_days: int | None = None) -> int:
    """Delete audit rows past the retention window.

    HIPAA 164.316(b)(2) requires six years of documentation retention, so the
    default window is deliberately long and a value of 0 disables purging
    entirely. Deleting audit history early is a compliance failure, so this
    never runs with an implicit short default.
    """
    days = (
        settings.audit_retention_days if retention_days is None else retention_days
    )
    if days <= 0:
        return 0
    pool = await init_pool()
    async with pool.acquire() as conn:
        status_line = await conn.execute(
            "DELETE FROM audit_events "
            "WHERE ts < now() - ($1::double precision * interval '1 day')",
            float(days),
        )
    try:
        removed = int(status_line.split()[-1])
    except (ValueError, IndexError):
        return 0
    if removed:
        logger.info(
            "purged expired audit rows",
            extra={"removed": removed, "retention_days": days},
        )
    return removed


async def _janitor_loop(interval: int) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            await purge_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("audit retention sweep failed", exc_info=True)


async def start_janitor() -> None:
    global _janitor
    if _janitor is not None and not _janitor.done():
        return
    interval = settings.audit_purge_interval_seconds
    if interval <= 0 or settings.audit_retention_days <= 0:
        return
    _janitor = asyncio.create_task(_janitor_loop(interval), name="audit-retention")


async def stop_janitor() -> None:
    global _janitor
    if _janitor is None:
        return
    _janitor.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _janitor
    _janitor = None


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


async def search(
    *,
    subject_ref: str | None = None,
    actor: str | None = None,
    outcome: str | None = None,
    resource_type: str | None = None,
    service: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    break_glass: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Search the audit index.

    ``subject_ref`` is an already-pseudonymised reference (see
    ``middleware.audit_reference``); it matches either the resource the request
    targeted or the patient a search was filtered by, because both are
    accesses to that person's record.

    Every value is bound as a query parameter — filters are user-supplied and
    must never be interpolated into SQL.
    """
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    offset = max(0, int(offset))

    where: list[str] = []
    args: list[Any] = []

    def add(clause_template: str, value: Any) -> None:
        args.append(value)
        where.append(clause_template.format(n=len(args)))

    if subject_ref:
        # A patient is "touched" by a request that targeted their record, or
        # by a search that returned them in its results. Leaving the third
        # case out would under-report disclosures in an accounting request.
        add(
            "(patient_ref = ${n} OR resource_ref = ${n} "
            "OR subject_refs @> ARRAY[${n}]::text[])",
            subject_ref,
        )
    if actor:
        add("actor_sub = ${n}", actor)
    if outcome:
        add("outcome = ${n}", outcome)
    if resource_type:
        # Path segments are lower-cased by some routes and canonical in others.
        add("lower(resource_type) = lower(${n})", resource_type)
    if service:
        add("service = ${n}", service)
    if since:
        add("ts >= ${n}", since)
    if until:
        add("ts <= ${n}", until)
    if break_glass is not None:
        add("break_glass = ${n}", break_glass)

    clause = f"WHERE {' AND '.join(where)}" if where else ""

    pool = await init_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM audit_events {clause}", *args
        )
        rows = await conn.fetch(
            f"""
            SELECT {", ".join(_COLUMNS)}
            FROM audit_events
            {clause}
            ORDER BY ts DESC, id DESC
            LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
            """,
            *args,
            limit,
            offset,
        )

    items = []
    for row in rows:
        item = dict(row)
        ts = item.get("ts")
        if isinstance(ts, datetime):
            item["ts"] = ts.isoformat()
        items.append(item)

    return {
        "items": items,
        "count": len(items),
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


async def actors_for_subject(subject_ref: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Distinct actors who touched a subject, newest access first.

    The direct answer to "who viewed MRN-X?", without paging through every
    individual request that person made.
    """
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    pool = await init_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT actor_sub,
                   count(*)                                   AS accesses,
                   max(ts)                                    AS last_access,
                   min(ts)                                    AS first_access,
                   count(*) FILTER (WHERE outcome = 'denied') AS denied,
                   count(*) FILTER (WHERE break_glass)        AS break_glass
            FROM audit_events
            WHERE (patient_ref = $1 OR resource_ref = $1
                   OR subject_refs @> ARRAY[$1]::text[])
              AND actor_sub IS NOT NULL
            GROUP BY actor_sub
            ORDER BY max(ts) DESC
            LIMIT $2
            """,
            subject_ref,
            limit,
        )
    result = []
    for row in rows:
        item = dict(row)
        for key in ("last_access", "first_access"):
            value = item.get(key)
            if isinstance(value, datetime):
                item[key] = value.isoformat()
        item["accesses"] = int(item.get("accesses") or 0)
        item["denied"] = int(item.get("denied") or 0)
        item["break_glass"] = int(item.get("break_glass") or 0)
        result.append(item)
    return result
