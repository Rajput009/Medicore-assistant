"""Gateway audit-search endpoints: authorisation, validation, wiring.

The store's SQL is covered against real Postgres in ``test_audit_search``.
These tests cover the HTTP contract around it — who may ask, what a bad
question returns, and that a raw MRN from the caller is hashed the same way
the audit writer hashed it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.common.security import create_access_token


@pytest.fixture()
def gateway(monkeypatch):
    """Gateway with the audit store stubbed; no database involved."""
    import backend.services.gateway.main as gw

    calls: dict[str, Any] = {"search": [], "accessors": []}

    async def fake_search(**kwargs):
        calls["search"].append(kwargs)
        return {
            "items": [
                {
                    "ts": "2026-07-27T10:00:00+00:00",
                    "actor_sub": "dr.smith",
                    "actor_roles": ["clinician"],
                    "method": "GET",
                    "path": "/fhir/patient/{id}",
                    "status": 200,
                    "outcome": "success",
                    "resource_type": "patient",
                    "resource_ref": "sha256:abc",
                    "patient_ref": None,
                    "service": "gateway",
                    "client_ip": "203.0.113.7",
                }
            ],
            "count": 1,
            "total": 1,
            "limit": kwargs.get("limit", 50),
            "offset": kwargs.get("offset", 0),
        }

    async def fake_actors(subject_ref, limit=50):
        calls["accessors"].append((subject_ref, limit))
        return [
            {
                "actor_sub": "dr.smith",
                "accesses": 3,
                "denied": 0,
                "first_access": "2026-07-20T09:00:00+00:00",
                "last_access": "2026-07-27T10:00:00+00:00",
            }
        ]

    monkeypatch.setattr(gw.audit_store, "search", fake_search)
    monkeypatch.setattr(gw.audit_store, "actors_for_subject", fake_actors)

    # Keep the FHIR/cache paths inert so this module never touches Postgres.
    async def no_cache(*a, **k):
        return None

    monkeypatch.setattr(gw, "get_cached", no_cache)
    monkeypatch.setattr(gw, "set_cached", no_cache)

    with TestClient(gw.app, raise_server_exceptions=False) as client:
        yield client, calls


def auth(roles):
    return {"Authorization": f"Bearer {create_access_token('u1', roles=roles)}"}


# ---------------------------------------------------------------------------
# Authorisation — knowing who looked at whom is itself sensitive
# ---------------------------------------------------------------------------


class TestAuthorisation:
    @pytest.mark.parametrize(
        "path",
        [
            "/audit/search",
            "/audit/patient/MRN-1/accessors",
            "/audit/stats",
        ],
    )
    def test_requires_authentication(self, gateway, path):
        client, _ = gateway
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize(
        "path",
        [
            "/audit/search",
            "/audit/patient/MRN-1/accessors",
            "/audit/stats",
        ],
    )
    def test_clinician_is_forbidden(self, gateway, path):
        """A clinician may read charts but must not see the surveillance view
        of who read which chart."""
        client, _ = gateway
        assert client.get(path, headers=auth(["clinician"])).status_code == 403

    def test_viewer_is_forbidden(self, gateway):
        client, _ = gateway
        assert client.get("/audit/search", headers=auth(["viewer"])).status_code == 403

    def test_admin_is_allowed(self, gateway):
        client, _ = gateway
        assert client.get("/audit/search", headers=auth(["admin"])).status_code == 200

    def test_the_audit_query_is_itself_audited(self, gateway, caplog):
        """Investigating the investigators has to work too."""
        import json
        import logging

        client, _ = gateway
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get("/audit/search?patient=MRN-1", headers=auth(["admin"]))
        records = [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "medicore.audit" and r.getMessage().startswith("{")
        ]
        assert any(r["path"] == "/audit/search" and r["sub"] == "u1" for r in records)

    def test_searching_does_not_log_the_raw_mrn(self, gateway, caplog):
        """The query parameter is an identifier; only its name may be logged."""
        import logging

        client, _ = gateway
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get("/audit/search?patient=MRN-SECRET", headers=auth(["admin"]))
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "MRN-SECRET" not in blob


# ---------------------------------------------------------------------------
# The hashing contract
# ---------------------------------------------------------------------------


class TestIdentifierHashing:
    def test_raw_identifier_is_hashed_before_matching(self, gateway):
        """Callers pass an MRN; the store only ever sees the pseudonym."""
        from backend.common.middleware import audit_reference

        client, calls = gateway
        client.get("/audit/search?patient=MRN-000123", headers=auth(["admin"]))
        assert calls["search"][-1]["subject_ref"] == audit_reference("MRN-000123")
        assert calls["search"][-1]["subject_ref"] != "MRN-000123"

    def test_response_echoes_the_pseudonym_for_cross_referencing(self, gateway):
        from backend.common.middleware import audit_reference

        client, _ = gateway
        body = client.get(
            "/audit/search?patient=MRN-000123", headers=auth(["admin"])
        ).json()
        assert body["subject_ref"] == audit_reference("MRN-000123")

    def test_accessors_route_hashes_the_path_parameter(self, gateway):
        from backend.common.middleware import audit_reference

        client, calls = gateway
        body = client.get(
            "/audit/patient/MRN-42/accessors", headers=auth(["admin"])
        ).json()
        assert calls["accessors"][-1][0] == audit_reference("MRN-42")
        assert body["patient_ref"] == audit_reference("MRN-42")

    def test_no_patient_filter_means_no_subject_ref(self, gateway):
        client, calls = gateway
        client.get("/audit/search?actor=dr.smith", headers=auth(["admin"]))
        assert calls["search"][-1]["subject_ref"] is None

    def test_blank_patient_is_treated_as_absent(self, gateway):
        client, calls = gateway
        client.get("/audit/search?patient=%20%20", headers=auth(["admin"]))
        assert calls["search"][-1]["subject_ref"] is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unknown_outcome_is_rejected(self, gateway):
        """An empty result set would look like 'nobody accessed this', which
        is the most dangerous possible answer to get wrong."""
        client, _ = gateway
        r = client.get("/audit/search?outcome=bogus", headers=auth(["admin"]))
        assert r.status_code == 400
        assert "outcome" in r.json()["detail"]

    @pytest.mark.parametrize("outcome", ["success", "failure", "denied", "error"])
    def test_valid_outcomes_are_accepted(self, gateway, outcome):
        client, _ = gateway
        r = client.get(f"/audit/search?outcome={outcome}", headers=auth(["admin"]))
        assert r.status_code == 200

    def test_reversed_time_range_is_rejected(self, gateway):
        client, _ = gateway
        r = client.get(
            "/audit/search?since=2026-07-27T00:00:00Z&until=2026-07-01T00:00:00Z",
            headers=auth(["admin"]),
        )
        assert r.status_code == 400
        assert "earlier" in r.json()["detail"]

    def test_excessive_window_is_rejected(self, gateway):
        """Bounds the most expensive query shape as the index grows."""
        client, _ = gateway
        r = client.get(
            "/audit/search?since=2019-01-01T00:00:00Z&until=2026-01-01T00:00:00Z",
            headers=auth(["admin"]),
        )
        assert r.status_code == 400
        assert "exceed" in r.json()["detail"]

    def test_limit_above_the_ceiling_is_rejected(self, gateway):
        client, _ = gateway
        assert (
            client.get("/audit/search?limit=100000", headers=auth(["admin"])).status_code
            == 422
        )

    def test_negative_offset_is_rejected(self, gateway):
        client, _ = gateway
        assert (
            client.get("/audit/search?offset=-1", headers=auth(["admin"])).status_code
            == 422
        )

    def test_malformed_patient_id_is_rejected_on_the_accessors_route(self, gateway):
        client, _ = gateway
        r = client.get("/audit/patient/..%2Fetc/accessors", headers=auth(["admin"]))
        assert r.status_code in (400, 404)

    def test_defaults_to_a_thirty_day_window(self, gateway):
        """An unbounded default would make the common case a full scan."""
        client, calls = gateway
        client.get("/audit/search", headers=auth(["admin"]))
        kwargs = calls["search"][-1]
        span = kwargs["until"] - kwargs["since"]
        assert timedelta(days=29) < span <= timedelta(days=30)

    def test_resolved_window_is_echoed_back(self, gateway):
        client, _ = gateway
        body = client.get("/audit/search", headers=auth(["admin"])).json()
        assert datetime.fromisoformat(body["since"]) < datetime.fromisoformat(
            body["until"]
        )

    def test_naive_datetimes_are_treated_as_utc(self, gateway):
        client, calls = gateway
        r = client.get(
            "/audit/search?since=2026-07-01T00:00:00&until=2026-07-10T00:00:00",
            headers=auth(["admin"]),
        )
        assert r.status_code == 200
        assert calls["search"][-1]["since"].tzinfo is not None


# ---------------------------------------------------------------------------
# Filter pass-through and failure handling
# ---------------------------------------------------------------------------


class TestFiltersAndFailures:
    def test_all_filters_reach_the_store(self, gateway):
        client, calls = gateway
        client.get(
            "/audit/search?actor=dr.a&outcome=denied&resource_type=Patient"
            "&service=gateway&limit=10&offset=5",
            headers=auth(["admin"]),
        )
        kwargs = calls["search"][-1]
        assert kwargs["actor"] == "dr.a"
        assert kwargs["outcome"] == "denied"
        assert kwargs["resource_type"] == "Patient"
        assert kwargs["service"] == "gateway"
        assert kwargs["limit"] == 10
        assert kwargs["offset"] == 5

    def test_results_are_returned_verbatim(self, gateway):
        client, _ = gateway
        body = client.get("/audit/search", headers=auth(["admin"])).json()
        assert body["total"] == 1
        assert body["items"][0]["actor_sub"] == "dr.smith"

    def test_accessor_summary_shape(self, gateway):
        client, _ = gateway
        body = client.get(
            "/audit/patient/MRN-42/accessors", headers=auth(["admin"])
        ).json()
        assert body["count"] == 1
        assert body["accessors"][0]["accesses"] == 3
        assert body["accessors"][0]["denied"] == 0

    def test_index_outage_returns_503_not_500(self, gateway, monkeypatch):
        """A broken audit index must not look like a broken gateway."""
        import backend.services.gateway.main as gw

        async def boom(**kwargs):
            raise RuntimeError("relation audit_events does not exist")

        monkeypatch.setattr(gw.audit_store, "search", boom)
        client, _ = gateway
        r = client.get("/audit/search", headers=auth(["admin"]))
        assert r.status_code == 503
        assert "unavailable" in r.json()["detail"].lower()

    def test_index_outage_does_not_leak_internals(self, gateway, monkeypatch):
        import backend.services.gateway.main as gw

        async def boom(**kwargs):
            raise RuntimeError("password=hunter2 host=db.internal")

        monkeypatch.setattr(gw.audit_store, "search", boom)
        client, _ = gateway
        body = client.get("/audit/search", headers=auth(["admin"])).text
        assert "hunter2" not in body
        assert "db.internal" not in body

    def test_accessors_outage_returns_503(self, gateway, monkeypatch):
        import backend.services.gateway.main as gw

        async def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(gw.audit_store, "actors_for_subject", boom)
        client, _ = gateway
        assert (
            client.get("/audit/patient/MRN-1/accessors", headers=auth(["admin"])).status_code
            == 503
        )


# ---------------------------------------------------------------------------
# Stats / readiness
# ---------------------------------------------------------------------------


class TestStatsAndReadiness:
    def test_stats_report_loss_counters(self, gateway):
        client, _ = gateway
        body = client.get("/audit/stats", headers=auth(["admin"])).json()
        for key in ("enabled", "retention_days", "running", "queued", "written",
                    "dropped", "failed"):
            assert key in body

    def test_ready_reports_the_index_without_requiring_auth(self, gateway):
        """Probes send no Authorization header."""
        client, _ = gateway
        r = client.get("/ready")
        assert r.status_code in (200, 503)
        assert "audit_index" in r.json()

    def test_ready_does_not_expose_counters_to_unauthenticated_callers(self, gateway):
        client, _ = gateway
        body = client.get("/ready").json()
        assert "dropped" not in body
        assert body["audit_index"] in ("ok", "degraded", "disabled")

    def test_a_stalled_index_does_not_fail_readiness(self, gateway, monkeypatch):
        """The log stream is the system of record; a stalled index must not
        pull a healthy pod out of the load balancer."""
        import backend.services.gateway.main as gw

        monkeypatch.setattr(
            gw.audit_store, "stats", lambda: {"running": False, "queued": 0}
        )

        async def ok():
            return None

        monkeypatch.setattr(gw, "cache_ping", ok)
        client, _ = gateway
        r = client.get("/ready")
        assert r.status_code == 200
        assert r.json()["audit_index"] == "degraded"


# ---------------------------------------------------------------------------
# The audit path itself stays non-blocking
# ---------------------------------------------------------------------------


class TestClinicalPathIsUnaffected:
    def test_a_failing_sink_does_not_fail_the_request(self, gateway, monkeypatch):
        """The whole design rests on this: audit indexing must never be able
        to break a clinical read."""
        from backend.common import middleware

        def exploding_sink(record):
            raise RuntimeError("sink is on fire")

        monkeypatch.setattr(middleware, "_sink", exploding_sink)
        client, _ = gateway
        assert client.get("/health").status_code == 200

    def test_records_reach_a_registered_sink(self, gateway, monkeypatch):
        from backend.common import middleware

        seen: list[dict] = []
        monkeypatch.setattr(middleware, "_sink", seen.append)
        client, _ = gateway
        client.get("/audit/stats", headers=auth(["admin"]))
        assert any(r.get("path") == "/audit/stats" for r in seen)

    def test_sink_receives_the_pseudonymised_reference(self, gateway, monkeypatch):
        from backend.common import middleware

        seen: list[dict] = []
        monkeypatch.setattr(middleware, "_sink", seen.append)
        client, _ = gateway
        client.get("/fhir/patient/MRN-99", headers=auth(["clinician"]))
        refs = [r.get("resource_ref") for r in seen if r.get("resource_ref")]
        assert refs
        assert all("MRN-99" not in str(ref) for ref in refs)


def test_audit_sink_can_be_cleared():
    """Shutdown must be able to detach the sink."""
    from backend.common import middleware

    middleware.set_audit_sink(lambda record: None)
    assert middleware._sink is not None
    middleware.set_audit_sink(None)
    assert middleware._sink is None
