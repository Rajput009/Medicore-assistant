"""The admin audit-search endpoint.

Covers who may search, that the raw MRN a caller supplies is hashed before it
reaches the index, input validation, and that reading the audit trail is
itself audited.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.common.security import create_access_token


@pytest.fixture()
def gateway(monkeypatch):
    import backend.services.gateway.main as gw

    calls: dict[str, Any] = {"search": []}

    async def fake_search(**kwargs):
        calls["search"].append(kwargs)
        return (
            [
                {
                    "occurred_at": "2026-07-27T10:00:00+00:00",
                    "actor_sub": "dr.smith",
                    "outcome": "success",
                    "patient_ref": kwargs.get("patient_ref"),
                }
            ],
            1,
        )

    async def no_cache(*a, **k):
        return None

    monkeypatch.setattr(gw, "audit_search_events", fake_search)
    monkeypatch.setattr(gw, "get_cached", no_cache)
    monkeypatch.setattr(gw, "set_cached", no_cache)

    with TestClient(gw.app, raise_server_exceptions=False) as client:
        yield client, calls


_ip_counter = itertools.count(1)


_ip_counter = itertools.count(1)


def auth(*roles: str) -> dict[str, str]:
    # Unique source IP per call. The in-process rate limiter buckets by client
    # IP and every TestClient request otherwise reports the same host, so a
    # module's requests would share (and exhaust) one budget in a full run.
    return {
        "Authorization": f"Bearer {create_access_token('admin1', roles=list(roles))}",
        "X-Forwarded-For": f"203.0.114.{next(_ip_counter) % 250 + 1}",
    }


class TestAccessControl:
    def test_requires_authentication(self, gateway):
        client, calls = gateway
        assert client.get("/audit/search").status_code == 401
        assert calls["search"] == []

    def test_clinicians_cannot_read_the_audit_trail(self, gateway):
        """Access logs are an admin/compliance function, not a clinical one."""
        client, calls = gateway
        r = client.get("/audit/search", headers=auth("clinician"))
        assert r.status_code == 403
        assert calls["search"] == []

    def test_viewers_cannot_read_the_audit_trail(self, gateway):
        client, _ = gateway
        assert client.get("/audit/search", headers=auth("viewer")).status_code == 403

    def test_admin_can_search(self, gateway):
        client, _ = gateway
        assert client.get("/audit/search", headers=auth("admin")).status_code == 200


class TestPatientLookup:
    def test_a_raw_mrn_is_hashed_before_it_reaches_the_index(self, gateway):
        client, calls = gateway
        r = client.get(
            "/audit/search", params={"patient": "MRN-000123"}, headers=auth("admin")
        )
        assert r.status_code == 200
        sent = calls["search"][0]["patient_ref"]
        assert sent != "MRN-000123"
        assert sent.startswith("sha256:")

    def test_the_hash_matches_what_the_middleware_stores(self, gateway):
        """If these ever diverge, every lookup silently returns nothing."""
        from backend.common.middleware import audit_reference

        client, calls = gateway
        client.get("/audit/search", params={"patient": "MRN-1"}, headers=auth("admin"))
        assert calls["search"][0]["patient_ref"] == audit_reference("MRN-1")

    def test_the_response_does_not_echo_the_raw_mrn(self, gateway):
        client, _ = gateway
        r = client.get(
            "/audit/search", params={"patient": "MRN-000123"}, headers=auth("admin")
        )
        assert "MRN-000123" not in r.text

    def test_omitting_the_patient_searches_everything(self, gateway):
        client, calls = gateway
        client.get("/audit/search", headers=auth("admin"))
        assert calls["search"][0]["patient_ref"] is None


class TestFiltersAndValidation:
    def test_actor_filter_is_passed_through(self, gateway):
        client, calls = gateway
        client.get("/audit/search", params={"actor": "dr.smith"}, headers=auth("admin"))
        assert calls["search"][0]["actor_sub"] == "dr.smith"

    def test_outcome_filter_is_passed_through(self, gateway):
        client, calls = gateway
        client.get("/audit/search", params={"outcome": "denied"}, headers=auth("admin"))
        assert calls["search"][0]["outcome"] == "denied"

    def test_an_unknown_outcome_is_rejected(self, gateway):
        client, calls = gateway
        r = client.get(
            "/audit/search", params={"outcome": "banana"}, headers=auth("admin")
        )
        assert r.status_code == 400
        assert calls["search"] == []

    def test_time_window_is_parsed(self, gateway):
        client, calls = gateway
        r = client.get(
            "/audit/search",
            params={"since": "2026-07-01T00:00:00Z"},
            headers=auth("admin"),
        )
        assert r.status_code == 200
        assert calls["search"][0]["since"] is not None

    def test_limit_is_capped(self, gateway):
        """One request must not be able to pull the entire trail."""
        client, _ = gateway
        r = client.get("/audit/search", params={"limit": 5000}, headers=auth("admin"))
        assert r.status_code == 422

    def test_pagination_is_passed_through(self, gateway):
        client, calls = gateway
        client.get(
            "/audit/search", params={"limit": 10, "offset": 20}, headers=auth("admin")
        )
        assert calls["search"][0]["limit"] == 10
        assert calls["search"][0]["offset"] == 20

    def test_reports_the_total_alongside_the_page(self, gateway):
        client, _ = gateway
        body = client.get("/audit/search", headers=auth("admin")).json()
        assert body["count"] == 1
        assert body["total"] == 1


class TestDegradation:
    def test_an_index_outage_is_a_503_not_a_500(self, gateway, monkeypatch):
        import backend.services.gateway.main as gw

        async def boom(**kwargs):
            raise RuntimeError("postgres://user:hunter2@db unreachable")

        monkeypatch.setattr(gw, "audit_search_events", boom)
        client, _ = gateway
        r = client.get("/audit/search", headers=auth("admin"))
        assert r.status_code == 503

    def test_index_errors_do_not_leak_connection_details(self, gateway, monkeypatch):
        import backend.services.gateway.main as gw

        async def boom(**kwargs):
            raise RuntimeError("postgres://user:hunter2@db unreachable")

        monkeypatch.setattr(gw, "audit_search_events", boom)
        client, _ = gateway
        assert "hunter2" not in client.get("/audit/search", headers=auth("admin")).text


class TestSearchingIsItselfAudited:
    def test_the_search_request_is_logged(self, gateway, caplog):
        """"Who went looking through the audit log?" must also be answerable."""
        import logging

        client, _ = gateway
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get(
                "/audit/search", params={"patient": "MRN-1"}, headers=auth("admin")
            )
        assert any("/audit/search" in record.message for record in caplog.records)

    def test_the_logged_search_does_not_contain_the_raw_mrn(self, gateway, caplog):
        import logging

        client, _ = gateway
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get(
                "/audit/search", params={"patient": "MRN-1"}, headers=auth("admin")
            )
        for record in caplog.records:
            if "/audit/search" in record.message:
                # Query *values* are never logged - only parameter names.
                assert "MRN-1" not in record.message
