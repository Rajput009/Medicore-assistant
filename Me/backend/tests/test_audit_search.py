"""Audit index tests against a genuine PostgreSQL server.

``pgserver`` ships a real PostgreSQL binary as a wheel, so these exercise the
actual DDL, ``text[]`` columns, partial index, batch insert path and the
grouping query behind "who viewed MRN-X?" — not a mock that would happily
accept SQL Postgres rejects.

Skipped cleanly when ``pgserver`` is not installed.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pgserver", reason="pgserver not installed")
pytest.importorskip("asyncpg", reason="asyncpg not installed")

import pgserver  # noqa: E402


@pytest.fixture(scope="module")
def pg_uri():
    """Boot one real Postgres for the whole module; tear it down after."""
    td = Path(tempfile.mkdtemp(prefix="medicore-pg-audit-"))
    server = pgserver.get_server(td, cleanup_mode="delete")
    yield server.get_uri()
    try:
        server._cleanup()
    except Exception:
        pass


@pytest.fixture()
def event_loop():
    """Own the loop; asyncio.Queue/Lock objects must not outlive it."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture()
def audit(pg_uri, event_loop, monkeypatch):
    """Bind the audit store to the embedded Postgres; isolate each test."""
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", pg_uri)
    monkeypatch.setenv("JWT_SECRET", "test-secret-at-least-32-chars-long!!")

    from backend.common import config

    fresh = config.Settings()
    monkeypatch.setattr(config, "settings", fresh)

    import backend.common.audit_store as store
    import backend.common.cache as cache

    monkeypatch.setattr(cache, "settings", fresh)
    monkeypatch.setattr(store, "settings", fresh)

    async def _shutdown():
        await store.stop()
        await cache.stop_janitor()
        await cache.close_pool()
        cache._pool = None
        cache._pool_lock = None
        cache._janitor_task = None
        store._writer = None
        store._janitor = None

    async def _prepare():
        await _shutdown()
        await store.ensure_schema()
        pool = await cache.init_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE audit_events")

    event_loop.run_until_complete(_prepare())
    yield store
    event_loop.run_until_complete(_shutdown())


def run(coro):
    return asyncio.get_event_loop_policy().get_event_loop().run_until_complete(coro)


def record(**overrides):
    """A representative audit record as the middleware emits it."""
    base = {
        "event": "http_request",
        "request_id": "req-1",
        "service": "gateway",
        "method": "GET",
        "path": "/fhir/patient/{id}",
        "query_keys": [],
        "client_ip": "203.0.113.7",
        "user_agent": "pytest",
        "status": 200,
        "outcome": "success",
        "duration_ms": 12.5,
        "sub": "dr.smith",
        "roles": ["clinician"],
        "resource_type": "patient",
        "resource_ref": "sha256:aaa",
    }
    base.update(overrides)
    return base


def insert(store, *records):
    """Write records synchronously, bypassing the queue for determinism.

    Uses a standalone writer so these tests do not depend on the module
    singleton's lifecycle; the SQL path exercised is identical.
    """
    writer = store.AuditWriter()
    run(writer._write([store.row_from_record(r) for r in records]))
    return writer


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_creates_table_and_indexes(self, audit):
        async def inspect():
            import backend.common.cache as cache

            pool = await cache.init_pool()
            async with pool.acquire() as conn:
                tables = await conn.fetch(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' AND tablename='audit_events'"
                )
                indexes = await conn.fetch(
                    "SELECT indexname FROM pg_indexes WHERE tablename='audit_events'"
                )
            return tables, [r["indexname"] for r in indexes]

        tables, names = run(inspect())
        assert tables, "audit_events was not created"
        # Each index backs a question an investigator actually asks.
        for expected in (
            "audit_events_patient_idx",
            "audit_events_resource_idx",
            "audit_events_actor_idx",
            "audit_events_ts_idx",
            "audit_events_denied_idx",
        ):
            assert any(expected in n for n in names), f"missing index {expected}"

    def test_ensure_schema_is_idempotent(self, audit):
        run(audit.ensure_schema())
        run(audit.ensure_schema())

    def test_upgrades_a_pre_existing_table_in_place(self, audit):
        """A deployed database already has audit_events without the
        break-glass columns. Upgrading must not require dropping audit
        history, which would itself be a compliance failure.
        """

        async def simulate_old_deployment():
            import backend.common.cache as cache

            pool = await cache.init_pool()
            async with pool.acquire() as conn:
                await conn.execute("DROP TABLE IF EXISTS audit_events")
                # The v1 shape, before break-glass existed.
                await conn.execute(
                    """
                    CREATE TABLE audit_events (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMP WITH TIME ZONE NOT NULL,
                        request_id TEXT, service TEXT, actor_sub TEXT,
                        actor_roles TEXT[], method TEXT NOT NULL,
                        path TEXT NOT NULL, status INTEGER, outcome TEXT,
                        resource_type TEXT, resource_ref TEXT,
                        patient_ref TEXT, bed_id TEXT, client_ip TEXT,
                        user_agent TEXT, duration_ms DOUBLE PRECISION,
                        query_keys TEXT[]
                    );
                    """
                )
                # A historical row that must survive the upgrade.
                await conn.execute(
                    "INSERT INTO audit_events (ts, method, path, actor_sub) "
                    "VALUES (now(), 'GET', '/fhir/patient/{id}', 'dr.legacy')"
                )

        run(simulate_old_deployment())
        run(audit.ensure_schema())

        # Old rows are intact and default to "not an override".
        legacy = run(audit.search(actor="dr.legacy"))
        assert legacy["total"] == 1
        assert legacy["items"][0]["break_glass"] is False

        # And new writes work against the upgraded table.
        insert(audit, record(sub="dr.new", break_glass=True, break_glass_reason="x" * 12))
        assert run(audit.search(break_glass=True))["total"] == 1

    def test_denied_index_is_partial(self, audit):
        """A full index on outcome would be mostly dead weight; denied rows
        are the small, high-signal slice worth indexing."""

        async def read_def():
            import backend.common.cache as cache

            pool = await cache.init_pool()
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'audit_events_denied_idx'"
                )

        assert "WHERE" in (run(read_def()) or "").upper()


# ---------------------------------------------------------------------------
# Row projection
# ---------------------------------------------------------------------------


class TestRowProjection:
    def test_maps_every_column(self, audit):
        row = audit.row_from_record(record())
        assert len(row) == 19
        assert row[3] == "dr.smith"
        assert row[4] == ["clinician"]
        assert row[7] == 200

    def test_break_glass_defaults_to_false(self, audit):
        """A missing flag must never read as an override."""
        row = audit.row_from_record(record())
        assert row[17] is False
        assert row[18] is None

    def test_break_glass_is_projected(self, audit):
        row = audit.row_from_record(
            record(break_glass=True, break_glass_reason="Arrest in bay 4")
        )
        assert row[17] is True
        assert row[18] == "Arrest in bay 4"

    def test_roles_string_is_coerced_to_array(self, audit):
        """IdPs emit roles as a list or a delimited string."""
        row = audit.row_from_record(record(roles="clinician"))
        assert row[4] == ["clinician"]

    def test_absent_optional_fields_become_null(self, audit):
        row = audit.row_from_record(
            {"method": "GET", "path": "/x", "status": 200}
        )
        assert row[3] is None  # actor_sub
        assert row[4] is None  # actor_roles
        assert row[10] is None  # resource_ref

    def test_oversized_values_are_truncated(self, audit):
        """A hostile client controls the user-agent; the column must not be
        the thing that decides whether an audit write succeeds."""
        row = audit.row_from_record(record(user_agent="x" * 5000, path="/" + "p" * 5000))
        assert len(row[14]) <= 256
        assert len(row[6]) <= 512

    def test_non_numeric_status_does_not_raise(self, audit):
        row = audit.row_from_record(record(status="weird", duration_ms="slow"))
        assert row[7] is None
        assert row[15] is None

    def test_probe_traffic_is_not_indexed(self, audit):
        for path in ("/health", "/ready", "/metrics", "/favicon.ico", "/ready/"):
            assert audit.should_index({"path": path}) is False
        assert audit.should_index({"path": "/fhir/patient/{id}"}) is True

    def test_write_round_trips_through_postgres(self, audit):
        """The projection must be accepted by the real column types."""
        insert(audit, record(query_keys=["patient", "date"]))

        async def read():
            import backend.common.cache as cache

            pool = await cache.init_pool()
            async with pool.acquire() as conn:
                return await conn.fetchrow("SELECT * FROM audit_events LIMIT 1")

        row = run(read())
        assert row["actor_roles"] == ["clinician"]
        assert row["query_keys"] == ["patient", "date"]
        assert isinstance(row["ts"], datetime)


# ---------------------------------------------------------------------------
# Writer: the request path must never pay for the audit index
# ---------------------------------------------------------------------------


class TestWriterIsNonBlocking:
    def test_submit_before_start_is_a_no_op(self, audit):
        writer = audit.AuditWriter()
        assert writer.submit(record()) is False

    def test_submit_never_raises_on_a_malformed_record(self, audit):
        async def go():
            writer = audit.AuditWriter()
            await writer.start()
            # roles is not a list/str/None; row projection must cope.
            assert writer.submit(record(roles=object())) in (True, False)
            await writer.stop()

        run(go())

    def test_full_queue_drops_and_counts_instead_of_blocking(self, audit):
        """Back-pressure onto a clinician is never the right answer; a counted
        drop is."""

        async def go():
            writer = audit.AuditWriter(queue_size=2)
            await writer.start()
            # Do not let the drain task run: fill the queue synchronously.
            accepted = [writer.submit(record()) for _ in range(6)]
            await writer.stop()
            return accepted, writer.dropped

        accepted, dropped = run(go())
        assert accepted[:2] == [True, True]
        assert dropped == 4
        assert accepted.count(False) == 4

    def test_probe_records_are_rejected_at_submit(self, audit):
        async def go():
            writer = audit.AuditWriter()
            await writer.start()
            result = writer.submit(record(path="/ready"))
            await writer.stop()
            return result

        assert run(go()) is False

    def test_queued_records_reach_postgres(self, audit):
        async def go():
            await audit.start()
            audit.submit(record(sub="dr.who", resource_ref="sha256:queued"))
            # Let the collector batch and flush.
            await asyncio.sleep(0.3)
            await audit.stop()
            return await audit.search(subject_ref="sha256:queued")

        result = run(go())
        assert result["total"] == 1
        assert result["items"][0]["actor_sub"] == "dr.who"

    def test_stop_flushes_what_is_still_queued(self, audit):
        """A pod being drained must not silently discard buffered records."""

        async def go():
            writer = audit.AuditWriter(flush_interval=30.0)
            await writer.start()
            for i in range(3):
                writer.submit(record(resource_ref=f"sha256:flush-{i}"))
            await writer.stop()
            return await audit.search(resource_type="patient")

        result = run(go())
        refs = {i["resource_ref"] for i in result["items"]}
        assert {"sha256:flush-0", "sha256:flush-1", "sha256:flush-2"} <= refs

    def test_write_failure_is_counted_not_raised(self, audit, monkeypatch):
        """A wedged database must degrade searchability, not the API."""

        async def boom():
            raise RuntimeError("database is down")

        async def go():
            writer = audit.AuditWriter()
            monkeypatch.setattr(audit, "init_pool", lambda: boom())
            await writer._write([audit.row_from_record(record())])
            return writer.failed

        assert run(go()) == 1

    def test_stats_expose_loss_counters(self, audit):
        """Dropped audit records are a compliance event; they must be
        observable rather than silent."""

        async def go():
            writer = audit.AuditWriter(queue_size=1)
            await writer.start()
            for _ in range(4):
                writer.submit(record())
            stats = writer.stats()
            await writer.stop()
            return stats

        stats = run(go())
        assert stats["capacity"] == 1
        assert stats["dropped"] == 3
        assert stats["running"] is True

    def test_start_is_idempotent(self, audit):
        async def go():
            writer = audit.AuditWriter()
            await writer.start()
            first = writer._task
            await writer.start()
            same = writer._task is first
            await writer.stop()
            return same

        assert run(go()) is True

    def test_batching_writes_multiple_records_in_one_round_trip(self, audit):
        async def go():
            writer = audit.AuditWriter(batch_size=50, flush_interval=0.1)
            await writer.start()
            for i in range(25):
                writer.submit(record(resource_ref=f"sha256:batch-{i}"))
            await asyncio.sleep(0.5)
            written = writer.written
            await writer.stop()
            return written

        assert run(go()) == 25


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_finds_accesses_by_patient_reference(self, audit):
        insert(
            audit,
            record(sub="dr.a", patient_ref="sha256:target"),
            record(sub="dr.b", patient_ref="sha256:other"),
        )
        result = run(audit.search(subject_ref="sha256:target"))
        assert result["total"] == 1
        assert result["items"][0]["actor_sub"] == "dr.a"

    def test_matches_resource_ref_as_well_as_patient_ref(self, audit):
        """Reading a chart directly and searching filtered by that patient are
        both accesses to the same person."""
        insert(
            audit,
            record(sub="dr.read", resource_ref="sha256:p1", patient_ref=None),
            record(sub="dr.search", resource_ref=None, patient_ref="sha256:p1"),
        )
        result = run(audit.search(subject_ref="sha256:p1"))
        assert {i["actor_sub"] for i in result["items"]} == {"dr.read", "dr.search"}

    def test_filters_by_actor(self, audit):
        insert(audit, record(sub="dr.a"), record(sub="dr.b"))
        result = run(audit.search(actor="dr.b"))
        assert result["total"] == 1

    def test_filters_by_outcome(self, audit):
        insert(
            audit,
            record(outcome="success", status=200),
            record(outcome="denied", status=403),
        )
        result = run(audit.search(outcome="denied"))
        assert result["total"] == 1
        assert result["items"][0]["status"] == 403

    def test_resource_type_match_is_case_insensitive(self, audit):
        """Routes disagree on casing; an investigator should not have to."""
        insert(audit, record(resource_type="patient"))
        assert run(audit.search(resource_type="Patient"))["total"] == 1
        assert run(audit.search(resource_type="patient"))["total"] == 1

    def test_filters_by_service(self, audit):
        insert(audit, record(service="gateway"), record(service="patient-flow"))
        assert run(audit.search(service="patient-flow"))["total"] == 1

    def test_time_window_bounds_results(self, audit):
        now = datetime.now(UTC)
        writer = audit.AuditWriter()
        run(
            writer._write(
                [
                    audit.row_from_record(record(sub="old"), now=now - timedelta(days=10)),
                    audit.row_from_record(record(sub="new"), now=now),
                ]
            )
        )
        recent = run(audit.search(since=now - timedelta(days=1)))
        assert [i["actor_sub"] for i in recent["items"]] == ["new"]

    def test_results_are_newest_first(self, audit):
        now = datetime.now(UTC)
        writer = audit.AuditWriter()
        run(
            writer._write(
                [
                    audit.row_from_record(record(sub="first"), now=now - timedelta(minutes=5)),
                    audit.row_from_record(record(sub="second"), now=now),
                ]
            )
        )
        result = run(audit.search())
        assert [i["actor_sub"] for i in result["items"]] == ["second", "first"]

    def test_pagination_reports_total_beyond_the_page(self, audit):
        insert(audit, *[record(sub=f"dr.{i}") for i in range(10)])
        page = run(audit.search(limit=3, offset=0))
        assert page["count"] == 3
        assert page["total"] == 10
        second = run(audit.search(limit=3, offset=3))
        assert {i["actor_sub"] for i in page["items"]} & {
            i["actor_sub"] for i in second["items"]
        } == set()

    def test_limit_is_clamped_to_the_hard_ceiling(self, audit):
        assert run(audit.search(limit=10_000))["limit"] == audit.MAX_SEARCH_LIMIT
        assert run(audit.search(limit=0))["limit"] == 1

    def test_filter_values_are_bound_not_interpolated(self, audit):
        """Filters are user input; a quote must be data, never syntax."""
        insert(audit, record(sub="dr.a"))
        hostile = "'; DROP TABLE audit_events; --"
        assert run(audit.search(actor=hostile))["total"] == 0
        # The table must still be there.
        assert run(audit.search())["total"] == 1

    def test_timestamps_are_serialised_as_iso8601(self, audit):
        insert(audit, record())
        item = run(audit.search())["items"][0]
        assert isinstance(item["ts"], str)
        assert datetime.fromisoformat(item["ts"])

    def test_no_filters_returns_everything_within_the_page(self, audit):
        insert(audit, *[record() for _ in range(4)])
        assert run(audit.search())["total"] == 4

    def test_break_glass_filter_isolates_overrides(self, audit):
        """'Show me every emergency override' is the review query."""
        insert(
            audit,
            record(sub="dr.normal"),
            record(
                sub="dr.emergency",
                break_glass=True,
                break_glass_reason="Arrest in bay 4",
            ),
        )
        only = run(audit.search(break_glass=True))
        assert only["total"] == 1
        assert only["items"][0]["actor_sub"] == "dr.emergency"
        assert only["items"][0]["break_glass_reason"] == "Arrest in bay 4"

        assert run(audit.search(break_glass=False))["total"] == 1
        # Unset means "either", not "non-override".
        assert run(audit.search())["total"] == 2


# ---------------------------------------------------------------------------
# "Who viewed MRN-X?" — the summarised answer
# ---------------------------------------------------------------------------


class TestAccessorSummary:
    def test_groups_accesses_by_clinician(self, audit):
        insert(
            audit,
            record(sub="dr.a", patient_ref="sha256:x"),
            record(sub="dr.a", patient_ref="sha256:x"),
            record(sub="dr.b", patient_ref="sha256:x"),
            record(sub="dr.c", patient_ref="sha256:other"),
        )
        rows = run(audit.actors_for_subject("sha256:x"))
        by_actor = {r["actor_sub"]: r for r in rows}
        assert set(by_actor) == {"dr.a", "dr.b"}
        assert by_actor["dr.a"]["accesses"] == 2
        assert by_actor["dr.b"]["accesses"] == 1

    def test_reports_first_and_last_access(self, audit):
        now = datetime.now(UTC)
        writer = audit.AuditWriter()
        run(
            writer._write(
                [
                    audit.row_from_record(
                        record(sub="dr.a", patient_ref="sha256:x"),
                        now=now - timedelta(hours=3),
                    ),
                    audit.row_from_record(
                        record(sub="dr.a", patient_ref="sha256:x"), now=now
                    ),
                ]
            )
        )
        row = run(audit.actors_for_subject("sha256:x"))[0]
        assert datetime.fromisoformat(row["first_access"]) < datetime.fromisoformat(
            row["last_access"]
        )

    def test_counts_denied_attempts_separately(self, audit):
        """An attempted access that was refused is the most interesting row in
        a privacy investigation."""
        insert(
            audit,
            record(sub="dr.snoop", patient_ref="sha256:x", outcome="denied", status=403),
            record(sub="dr.snoop", patient_ref="sha256:x", outcome="success"),
        )
        row = run(audit.actors_for_subject("sha256:x"))[0]
        assert row["accesses"] == 2
        assert row["denied"] == 1

    def test_ordered_by_most_recent_access(self, audit):
        now = datetime.now(UTC)
        writer = audit.AuditWriter()
        run(
            writer._write(
                [
                    audit.row_from_record(
                        record(sub="dr.old", patient_ref="sha256:x"),
                        now=now - timedelta(days=2),
                    ),
                    audit.row_from_record(
                        record(sub="dr.recent", patient_ref="sha256:x"), now=now
                    ),
                ]
            )
        )
        rows = run(audit.actors_for_subject("sha256:x"))
        assert [r["actor_sub"] for r in rows] == ["dr.recent", "dr.old"]

    def test_anonymous_requests_are_excluded(self, audit):
        """A request with no authenticated actor answers no "who" question."""
        insert(
            audit,
            record(sub=None, patient_ref="sha256:x", outcome="denied", status=401),
            record(sub="dr.a", patient_ref="sha256:x"),
        )
        rows = run(audit.actors_for_subject("sha256:x"))
        assert [r["actor_sub"] for r in rows] == ["dr.a"]

    def test_counts_break_glass_overrides_per_clinician(self, audit):
        """Whether someone reached this chart by override is a first-order
        question about their access, not a footnote."""
        insert(
            audit,
            record(sub="dr.emergency", patient_ref="sha256:x", break_glass=True),
            record(sub="dr.emergency", patient_ref="sha256:x"),
            record(sub="dr.routine", patient_ref="sha256:x"),
        )
        rows = {r["actor_sub"]: r for r in run(audit.actors_for_subject("sha256:x"))}
        assert rows["dr.emergency"]["break_glass"] == 1
        assert rows["dr.emergency"]["accesses"] == 2
        assert rows["dr.routine"]["break_glass"] == 0

    def test_unknown_subject_returns_empty(self, audit):
        assert run(audit.actors_for_subject("sha256:nobody")) == []


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention:
    def test_purges_only_rows_past_the_window(self, audit):
        now = datetime.now(UTC)
        writer = audit.AuditWriter()
        run(
            writer._write(
                [
                    audit.row_from_record(record(sub="ancient"), now=now - timedelta(days=400)),
                    audit.row_from_record(record(sub="recent"), now=now),
                ]
            )
        )
        assert run(audit.purge_expired(retention_days=365)) == 1
        remaining = run(audit.search())
        assert [i["actor_sub"] for i in remaining["items"]] == ["recent"]

    def test_zero_retention_never_deletes(self, audit):
        """0 means 'retain forever'. Deleting audit history early is a
        compliance failure, so it must not be the accidental default."""
        insert(audit, record())
        assert run(audit.purge_expired(retention_days=0)) == 0
        assert run(audit.search())["total"] == 1

    def test_default_retention_is_at_least_six_years(self, audit):
        """HIPAA 164.316(b)(2)(i): six-year documentation retention."""
        assert audit.settings.audit_retention_days >= 6 * 365

    def test_janitor_does_not_start_when_retention_is_disabled(self, audit, monkeypatch):
        monkeypatch.setattr(audit.settings, "audit_retention_days", 0)

        async def go():
            await audit.start_janitor()
            running = audit._janitor is not None
            await audit.stop_janitor()
            return running

        assert run(go()) is False

    def test_janitor_starts_and_stops(self, audit, monkeypatch):
        monkeypatch.setattr(audit.settings, "audit_purge_interval_seconds", 3600)

        async def go():
            await audit.start_janitor()
            running = audit._janitor is not None and not audit._janitor.done()
            await audit.start_janitor()  # idempotent
            await audit.stop_janitor()
            return running, audit._janitor

        running, after = run(go())
        assert running is True
        assert after is None


# ---------------------------------------------------------------------------
# PHI protection
# ---------------------------------------------------------------------------


class TestNoRawIdentifiers:
    def test_index_stores_only_the_pseudonymised_reference(self, audit):
        """The middleware hashes before the record ever reaches this module,
        so the table must never contain an MRN under default settings."""
        from backend.common.middleware import audit_reference

        ref = audit_reference("MRN-000123")
        assert "MRN" not in ref
        insert(audit, record(patient_ref=ref, resource_ref=ref))

        async def dump():
            import backend.common.cache as cache

            pool = await cache.init_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM audit_events")
            return "\n".join(str(dict(r)) for r in rows)

        assert "MRN-000123" not in run(dump())

    def test_search_by_raw_id_matches_the_stored_hash(self, audit):
        """The endpoint hashes the caller's MRN with the same salt; if these
        two ever diverge, every search silently returns nothing."""
        from backend.common.middleware import audit_reference

        insert(audit, record(patient_ref=audit_reference("MRN-77")))
        found = run(audit.search(subject_ref=audit_reference("MRN-77")))
        assert found["total"] == 1
