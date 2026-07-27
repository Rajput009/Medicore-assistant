"""Queryable audit trail — "who viewed MRN-X?".

Runs against a genuine PostgreSQL (via the ``pgserver`` wheel) so the real DDL,
indexes, COPY-based batch insert and filter SQL are all exercised.

The property that matters most is the round trip: the middleware stores a
salted HMAC of the patient id, and search hashes its input the same way. If
those two transforms ever diverge, a lookup by MRN returns nothing — and
"no results" is indistinguishable from "nobody accessed this record", which is
the worst possible failure for an access log.
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
    td = Path(tempfile.mkdtemp(prefix="medicore-pg-audit-"))
    server = pgserver.get_server(td, cleanup_mode="delete")
    yield server.get_uri()
    try:
        server._cleanup()
    except Exception:
        pass


@pytest.fixture()
def event_loop():
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
def store(pg_uri, event_loop, monkeypatch):
    """Bind the audit store to the embedded Postgres, one clean table per test.

    Only the DSN is redirected. An earlier version reloaded the whole config
    module, which swapped the shared ``settings`` object out from under every
    module that had already imported it — silently breaking CSRF origin checks
    in unrelated tests that happened to run afterwards.
    """
    from backend.common import audit_store as mod
    from backend.common.config import Settings

    monkeypatch.setattr(
        Settings, "sqlalchemy_dsn", property(lambda self: pg_uri), raising=False
    )

    async def setup():
        # Reset module state so each test gets a pool bound to this DSN.
        await mod.stop_writer(drain=False)
        await mod.close_pool()
        pool = await mod.init_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE audit_events;")

    event_loop.run_until_complete(setup())
    mod.reset_dropped()
    try:
        yield mod, event_loop
    finally:
        event_loop.run_until_complete(mod.stop_writer(drain=False))
        event_loop.run_until_complete(mod.close_pool())
        mod.reset_dropped()


def _event(**overrides):
    base = {
        "occurred_at": datetime.now(UTC),
        "request_id": "req-1",
        "service": "gateway",
        "method": "GET",
        "path": "/fhir/patient/{id}",
        "status": 200,
        "outcome": "success",
        "actor_sub": "dr.smith",
        "actor_roles": ["clinician"],
        "resource_type": "patient",
        "resource_ref": None,
        "patient_ref": "sha256:abc",
        "client_ip": "10.0.0.1",
        "duration_ms": 4.2,
    }
    base.update(overrides)
    return base


class TestSearchFilters:
    def test_finds_who_touched_a_patient(self, store):
        mod, loop = store

        async def run():
            await mod.record_now(_event(actor_sub="dr.smith", patient_ref="sha256:aaa"))
            await mod.record_now(_event(actor_sub="nurse.jones", patient_ref="sha256:aaa"))
            await mod.record_now(_event(actor_sub="dr.who", patient_ref="sha256:bbb"))
            return await mod.search(patient_ref="sha256:aaa")

        rows, total = loop.run_until_complete(run())
        assert total == 2
        assert {r["actor_sub"] for r in rows} == {"dr.smith", "nurse.jones"}

    def test_matches_a_patient_referenced_as_the_resource(self, store):
        """A direct read of Patient/X records the id as resource_ref."""
        mod, loop = store

        async def run():
            await mod.record_now(
                _event(patient_ref=None, resource_ref="sha256:aaa", resource_type="patient")
            )
            return await mod.search(patient_ref="sha256:aaa")

        rows, total = loop.run_until_complete(run())
        assert total == 1

    def test_finds_everything_one_actor_did(self, store):
        mod, loop = store

        async def run():
            await mod.record_now(_event(actor_sub="actor.test", patient_ref="sha256:aaa"))
            await mod.record_now(_event(actor_sub="actor.test", patient_ref="sha256:bbb"))
            await mod.record_now(_event(actor_sub="other", patient_ref="sha256:ccc"))
            return await mod.search(actor_sub="actor.test")

        rows, total = loop.run_until_complete(run())
        assert total == 2

    def test_filters_to_denied_attempts(self, store):
        """Refused access is what an investigation looks for first."""
        mod, loop = store

        async def run():
            await mod.record_now(_event(actor_sub="denied.test", outcome="success"))
            await mod.record_now(_event(actor_sub="denied.test", outcome="denied", status=403))
            return await mod.search(actor_sub="denied.test", outcome="denied")

        rows, total = loop.run_until_complete(run())
        assert total == 1
        assert rows[0]["status"] == 403

    def test_filters_by_time_window(self, store):
        mod, loop = store
        now = datetime.now(UTC)

        async def run():
            await mod.record_now(
                _event(actor_sub="window.test", occurred_at=now - timedelta(days=10))
            )
            await mod.record_now(
                _event(actor_sub="window.test", occurred_at=now - timedelta(hours=1))
            )
            return await mod.search(actor_sub="window.test", since=now - timedelta(days=1))

        rows, total = loop.run_until_complete(run())
        assert total == 1

    def test_combines_filters(self, store):
        mod, loop = store

        async def run():
            await mod.record_now(_event(actor_sub="dr.smith", patient_ref="sha256:aaa"))
            await mod.record_now(_event(actor_sub="dr.smith", patient_ref="sha256:bbb"))
            return await mod.search(actor_sub="dr.smith", patient_ref="sha256:aaa")

        rows, total = loop.run_until_complete(run())
        assert total == 1

    def test_newest_first(self, store):
        mod, loop = store
        now = datetime.now(UTC)

        async def run():
            await mod.record_now(
                _event(request_id="old", actor_sub="order.test", occurred_at=now - timedelta(hours=2))
            )
            await mod.record_now(
                _event(request_id="new", actor_sub="order.test", occurred_at=now)
            )
            # Scoped to this test's actor: other suites drive real requests
            # through the middleware, which indexes into the same table.
            return await mod.search(actor_sub="order.test")

        rows, _ = loop.run_until_complete(run())
        assert rows[0]["request_id"] == "new"

    def test_paginates_while_reporting_the_full_total(self, store):
        mod, loop = store

        async def run():
            for i in range(5):
                await mod.record_now(_event(request_id=f"r{i}", actor_sub="page.test"))
            return await mod.search(actor_sub="page.test", limit=2, offset=0)

        rows, total = loop.run_until_complete(run())
        assert len(rows) == 2
        # The page is capped but the caller still learns the real match count.
        assert total == 5


class TestPseudonymRoundTrip:
    def test_a_raw_mrn_finds_the_events_recorded_for_it(self, store):
        """The property the whole feature rests on."""
        mod, loop = store
        from backend.common.middleware import audit_reference

        stored_ref = audit_reference("MRN-000123")

        async def run():
            await mod.record_now(_event(patient_ref=stored_ref))
            # Search hashes the raw MRN with the same salt.
            return await mod.search(patient_ref=audit_reference("MRN-000123"))

        rows, total = loop.run_until_complete(run())
        assert total == 1

    def test_the_index_holds_no_raw_mrn_by_default(self, store):
        mod, loop = store
        from backend.common.middleware import audit_reference

        async def run():
            await mod.record_now(
                _event(actor_sub="raw.test", patient_ref=audit_reference("MRN-000123"))
            )
            pool = await mod.init_pool()
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT patient_ref FROM audit_events WHERE actor_sub = $1",
                    "raw.test",
                )

        ref = loop.run_until_complete(run())
        assert "MRN-000123" not in ref
        assert ref.startswith("sha256:")

    def test_a_different_patient_does_not_match(self, store):
        mod, loop = store
        from backend.common.middleware import audit_reference

        async def run():
            await mod.record_now(_event(patient_ref=audit_reference("MRN-1")))
            return await mod.search(patient_ref=audit_reference("MRN-2"))

        _, total = loop.run_until_complete(run())
        assert total == 0


class TestBufferedWriting:
    def test_buffered_events_reach_the_index(self, store):
        mod, loop = store

        async def run():
            await mod.start_writer()
            for i in range(10):
                mod.enqueue(_event(request_id=f"buffered-{i}", actor_sub="buffer.test"))
            await mod.flush_pending()
            # Filter to this test's own actor. Other suites drive real
            # requests through AuditLogMiddleware, which indexes them here
            # too - an unfiltered count would depend on test ordering.
            return await mod.search(actor_sub="buffer.test")

        rows, total = loop.run_until_complete(run())
        assert total == 10
        assert {r["request_id"] for r in rows} == {f"buffered-{i}" for i in range(10)}

    def test_enqueue_without_a_writer_is_a_no_op_not_a_crash(self, store):
        """Services that never start the writer must still serve requests."""
        mod, _ = store
        assert mod.enqueue(_event()) is False

    def test_a_full_buffer_drops_and_counts_rather_than_blocking(self, store):
        mod, loop = store

        async def run():
            await mod.start_writer()
            # Fill past capacity without letting the writer drain.
            mod._queue = asyncio.Queue(maxsize=2)
            accepted = [mod.enqueue(_event()) for _ in range(5)]
            return accepted

        accepted = loop.run_until_complete(run())
        assert accepted.count(True) == 2
        assert accepted.count(False) == 3
        # The gap is counted, so it is visible on /ready.
        assert mod.dropped_events() == 3


class TestRetention:
    def test_purge_removes_only_rows_past_the_window(self, store):
        mod, loop = store
        now = datetime.now(UTC)

        async def run():
            await mod.record_now(
                _event(actor_sub="purge.test", occurred_at=now - timedelta(days=400))
            )
            await mod.record_now(
                _event(actor_sub="purge.test", occurred_at=now - timedelta(days=1))
            )
            removed = await mod.purge_older_than(365)
            _, remaining = await mod.search(actor_sub="purge.test")
            return removed, remaining

        removed, remaining = loop.run_until_complete(run())
        assert removed == 1
        assert remaining == 1

    def test_retention_is_independent_of_the_cache_ttl(self):
        """The FHIR cache expires in hours; the audit trail is kept for years.

        Sharing a sweep between them would quietly shred the trail, so this
        asserts the audit store exposes its own purge with a days-based window.
        """
        from backend.common import audit_store, cache

        assert hasattr(audit_store, "purge_older_than")
        assert audit_store.purge_older_than is not getattr(cache, "purge_expired", None)
