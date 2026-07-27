"""Cache layer tests against a genuine PostgreSQL server.

``pgserver`` ships a real PostgreSQL binary as a wheel, so these tests exercise
the actual DDL, jsonb codec, ON CONFLICT upserts, SQL interval arithmetic and
the janitor without Docker or a system install.

Skipped cleanly when ``pgserver`` is not installed.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pgserver", reason="pgserver not installed")
pytest.importorskip("asyncpg", reason="asyncpg not installed")

import pgserver  # noqa: E402


@pytest.fixture(scope="module")
def pg_uri():
    """Boot one real Postgres for the whole module; tear it down after."""
    td = Path(tempfile.mkdtemp(prefix="medicore-pg-cache-"))
    server = pgserver.get_server(td, cleanup_mode="delete")
    yield server.get_uri()
    try:
        server._cleanup()
    except Exception:
        pass


@pytest.fixture()
def event_loop():
    """Own the loop; asyncio.Lock objects must not outlive it."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        # Cancel anything still pending before the loop dies.
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture()
def cache_mod(pg_uri, event_loop, monkeypatch):
    """Bind the real cache module to the embedded Postgres; isolate each test."""
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", pg_uri)
    monkeypatch.setenv("JWT_SECRET", "test-secret-at-least-32-chars-long!!")

    from backend.common import config

    fresh = config.Settings()
    monkeypatch.setattr(config, "settings", fresh)

    import backend.common.cache as cache

    monkeypatch.setattr(cache, "settings", fresh)

    async def _shutdown():
        await cache.stop_janitor()
        await cache.close_pool()
        # Drop the loop-bound lock so the next test's loop can create a fresh one.
        cache._pool = None
        cache._pool_lock = None
        cache._janitor_task = None

    async def _prepare():
        await _shutdown()
        pool = await cache.init_pool()
        async with pool.acquire() as conn:
            # Isolate tests that share one server: wipe rows, keep schema.
            await conn.execute("TRUNCATE fhir_cache")

    event_loop.run_until_complete(_prepare())
    yield cache
    event_loop.run_until_complete(_shutdown())


def run(coro):
    return asyncio.get_event_loop_policy().get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# DDL / pool lifecycle
# ---------------------------------------------------------------------------


class TestPoolAndDDL:
    def test_init_pool_creates_table_and_indexes(self, cache_mod):
        pool = run(cache_mod.init_pool())
        assert pool is not None

        async def inspect():
            async with pool.acquire() as conn:
                tables = await conn.fetch(
                    """
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public' AND tablename = 'fhir_cache'
                    """
                )
                indexes = await conn.fetch(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'fhir_cache'
                    """
                )
                return tables, [r["indexname"] for r in indexes]

        tables, index_names = run(inspect())
        assert tables, "fhir_cache table was not created"
        assert any("fhir_cache_resource_idx" in n for n in index_names)
        assert any("fhir_cache_fetched_at_idx" in n for n in index_names)

    def test_init_pool_is_idempotent(self, cache_mod):
        a = run(cache_mod.init_pool())
        b = run(cache_mod.init_pool())
        assert a is b

    def test_close_pool_releases_and_reopens(self, cache_mod):
        run(cache_mod.init_pool())
        run(cache_mod.close_pool())
        assert cache_mod._pool is None
        run(cache_mod.set_cached("Patient", {"id": "reopen"}, {"ok": True}))
        assert run(cache_mod.get_cached("Patient", {"id": "reopen"})) == {"ok": True}

    def test_ping_succeeds_against_live_server(self, cache_mod):
        run(cache_mod.ping())


# ---------------------------------------------------------------------------
# jsonb codec — the defect this module originally shipped with
# ---------------------------------------------------------------------------


class TestJsonbCodec:
    def test_dict_round_trip(self, cache_mod):
        payload = {
            "resourceType": "Bundle",
            "total": 2,
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "p1"}},
                {"resource": {"resourceType": "Patient", "id": "p2"}},
            ],
        }
        run(cache_mod.set_cached("Patient", {"name": "smith"}, payload))
        got = run(cache_mod.get_cached("Patient", {"name": "smith"}))
        assert got == payload
        assert isinstance(got, dict)
        assert isinstance(got["entry"], list)

    def test_nested_types_survive_codec(self, cache_mod):
        payload = {
            "flag": True,
            "count": 0,
            "ratio": 1.5,
            "tags": ["a", "b"],
            "meta": {"nested": {"deep": None}},
        }
        run(cache_mod.set_cached("Observation", {"id": "n1"}, payload))
        assert run(cache_mod.get_cached("Observation", {"id": "n1"})) == payload

    def test_params_column_is_jsonb_not_text(self, cache_mod):
        """params is written as a dict; the codec must encode it for jsonb."""
        run(cache_mod.set_cached("Patient", {"patient": "p-9", "code": "x"}, {"v": 1}))

        async def read_raw():
            pool = await cache_mod.init_pool()
            async with pool.acquire() as conn:
                return await conn.fetchrow(
                    "SELECT params, jsonb_typeof(params) AS kind, key "
                    "FROM fhir_cache WHERE key = $1",
                    cache_mod._make_key("Patient", {"patient": "p-9", "code": "x"}),
                )

        row = run(read_raw())
        assert row is not None
        assert row["kind"] == "object"
        assert isinstance(row["params"], dict)
        assert row["params"]["patient"] == "p-9"
        assert row["params"]["code"] == "x"


# ---------------------------------------------------------------------------
# Keys, upsert, TTL
# ---------------------------------------------------------------------------


class TestKeysAndUpsert:
    def test_key_order_independence(self, cache_mod):
        run(
            cache_mod.set_cached(
                "Observation",
                {"patient": "p1", "code": "hr", "date": "2024"},
                {"value": 72},
            )
        )
        got = run(
            cache_mod.get_cached(
                "Observation",
                {"date": "2024", "code": "hr", "patient": "p1"},
            )
        )
        assert got == {"value": 72}

    def test_empty_params_key(self, cache_mod):
        run(cache_mod.set_cached("CapabilityStatement", {}, {"status": "active"}))
        assert run(cache_mod.get_cached("CapabilityStatement", {})) == {
            "status": "active"
        }

    def test_on_conflict_upsert_overwrites_response_and_timestamp(self, cache_mod):
        run(cache_mod.set_cached("Patient", {"id": "u1"}, {"v": 1}))
        assert run(cache_mod.get_cached("Patient", {"id": "u1"})) == {"v": 1}

        run(cache_mod.set_cached("Patient", {"id": "u1"}, {"v": 2, "name": "Ada"}))
        assert run(cache_mod.get_cached("Patient", {"id": "u1"})) == {
            "v": 2,
            "name": "Ada",
        }

        async def count_rows():
            pool = await cache_mod.init_pool()
            async with pool.acquire() as conn:
                return await conn.fetchval("SELECT count(*) FROM fhir_cache")

        # Upsert must not insert a second row for the same key.
        assert run(count_rows()) == 1


class TestTTL:
    def test_fresh_entry_is_returned(self, cache_mod):
        run(cache_mod.set_cached("Patient", {"id": "ttl-fresh"}, {"ok": True}))
        assert (
            run(cache_mod.get_cached("Patient", {"id": "ttl-fresh"}, max_age_seconds=60))
            == {"ok": True}
        )

    def test_expired_entry_is_invisible_to_get(self, cache_mod):
        """TTL is evaluated in SQL via interval arithmetic, not in Python."""
        run(cache_mod.set_cached("Patient", {"id": "ttl-old"}, {"ok": True}))
        assert (
            run(cache_mod.get_cached("Patient", {"id": "ttl-old"}, max_age_seconds=0))
            is None
        )

    def test_sql_interval_honours_wall_clock_age(self, cache_mod):
        run(cache_mod.set_cached("Patient", {"id": "ttl-wait"}, {"ok": True}))

        async def age_row():
            pool = await cache_mod.init_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE fhir_cache SET fetched_at = now() - interval '10 seconds' "
                    "WHERE key = $1",
                    cache_mod._make_key("Patient", {"id": "ttl-wait"}),
                )

        run(age_row())
        assert (
            run(
                cache_mod.get_cached(
                    "Patient", {"id": "ttl-wait"}, max_age_seconds=5
                )
            )
            is None
        )
        assert (
            run(
                cache_mod.get_cached(
                    "Patient", {"id": "ttl-wait"}, max_age_seconds=60
                )
            )
            == {"ok": True}
        )


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


class TestInvalidation:
    def test_patient_scoped_invalidate_keeps_other_patients(self, cache_mod):
        run(
            cache_mod.set_cached(
                "Observation", {"patient": "p-keep", "code": "hr"}, {"v": 1}
            )
        )
        run(
            cache_mod.set_cached(
                "Observation", {"patient": "p-drop", "code": "hr"}, {"v": 2}
            )
        )
        deleted = run(cache_mod.invalidate_cache("Observation", patient_id="p-drop"))
        assert deleted == 1
        assert (
            run(
                cache_mod.get_cached(
                    "Observation", {"patient": "p-keep", "code": "hr"}
                )
            )
            == {"v": 1}
        )
        assert (
            run(
                cache_mod.get_cached(
                    "Observation", {"patient": "p-drop", "code": "hr"}
                )
            )
            is None
        )

    def test_resource_wide_invalidate(self, cache_mod):
        run(cache_mod.set_cached("Patient", {"id": "a"}, {"a": 1}))
        run(cache_mod.set_cached("Patient", {"id": "b"}, {"b": 1}))
        run(cache_mod.set_cached("Observation", {"id": "c"}, {"c": 1}))
        deleted = run(cache_mod.invalidate_cache("Patient"))
        assert deleted == 2
        assert run(cache_mod.get_cached("Patient", {"id": "a"})) is None
        assert run(cache_mod.get_cached("Observation", {"id": "c"})) == {"c": 1}

    def test_invalidate_unknown_returns_zero(self, cache_mod):
        assert run(cache_mod.invalidate_cache("DoesNotExist")) == 0


# ---------------------------------------------------------------------------
# Janitor / purge
# ---------------------------------------------------------------------------


class TestPurge:
    def test_purge_expired_removes_only_stale_rows(self, cache_mod):
        run(cache_mod.set_cached("Patient", {"id": "stale-1"}, {"s": 1}))
        run(cache_mod.set_cached("Patient", {"id": "stale-2"}, {"s": 2}))

        async def age_stale():
            pool = await cache_mod.init_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE fhir_cache SET fetched_at = now() - interval '1 hour'"
                )

        run(age_stale())
        run(cache_mod.set_cached("Patient", {"id": "fresh"}, {"f": 1}))

        purged = run(cache_mod.purge_expired(max_age_seconds=60))
        assert purged == 2
        assert run(cache_mod.get_cached("Patient", {"id": "fresh"})) == {"f": 1}
        assert run(cache_mod.get_cached("Patient", {"id": "stale-1"})) is None

    def test_purge_with_zero_ttl_clears_everything(self, cache_mod):
        run(cache_mod.set_cached("Patient", {"id": "z1"}, {"z": 1}))
        run(cache_mod.set_cached("Patient", {"id": "z2"}, {"z": 2}))
        assert run(cache_mod.purge_expired(max_age_seconds=0)) == 2


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_writes_to_distinct_keys(self, cache_mod):
        async def scenario():
            # Ensure pool (and its loop-bound lock) exist on *this* loop first.
            await cache_mod.init_pool()
            await asyncio.gather(
                *[
                    cache_mod.set_cached(
                        "Patient", {"id": f"c-{i}"}, {"id": f"c-{i}"}
                    )
                    for i in range(20)
                ]
            )
            return await asyncio.gather(
                *[
                    cache_mod.get_cached("Patient", {"id": f"c-{i}"})
                    for i in range(20)
                ]
            )

        results = run(scenario())
        assert results == [{"id": f"c-{i}"} for i in range(20)]

    def test_concurrent_upserts_same_key_leave_one_row(self, cache_mod):
        async def scenario():
            await cache_mod.init_pool()
            await asyncio.gather(
                *[
                    cache_mod.set_cached("Patient", {"id": "same"}, {"n": i})
                    for i in range(30)
                ]
            )
            pool = await cache_mod.init_pool()
            async with pool.acquire() as conn:
                count = await conn.fetchval("SELECT count(*) FROM fhir_cache")
                row = await conn.fetchrow(
                    "SELECT response FROM fhir_cache WHERE key = $1",
                    cache_mod._make_key("Patient", {"id": "same"}),
                )
            return count, row["response"] if row else None

        count, response = run(scenario())
        assert count == 1
        assert response is not None
        assert "n" in response


# ---------------------------------------------------------------------------
# Janitor task lifecycle
# ---------------------------------------------------------------------------


class TestJanitorLifecycle:
    def test_start_and_stop_janitor(self, cache_mod, monkeypatch):
        monkeypatch.setattr(cache_mod.settings, "cache_cleanup_interval_seconds", 3600)
        run(cache_mod.start_janitor())
        assert cache_mod._janitor_task is not None
        assert not cache_mod._janitor_task.done()
        # Idempotent.
        run(cache_mod.start_janitor())
        run(cache_mod.stop_janitor())
        assert cache_mod._janitor_task is None
