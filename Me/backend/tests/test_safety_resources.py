"""AllergyIntolerance and Condition routes on the gateway.

These are the resources a clinician checks before acting, so they get the same
treatment as the rest of the FHIR surface: role enforcement, the search-param
allow-list, id validation, and a cache TTL. Allergies get a shorter TTL than
other reference data because a stale allergy list is the kind of error that
harms someone.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.common.security import create_access_token


@pytest.fixture()
def gateway(monkeypatch):
    import backend.services.gateway.main as gw

    calls: dict[str, Any] = {"read": [], "search": []}

    async def fake_read(resource, resource_id):
        calls["read"].append((resource, resource_id))
        return {"resourceType": resource, "id": resource_id}

    async def fake_search(resource, params=None):
        calls["search"].append((resource, dict(params or {})))
        return {"resourceType": "Bundle", "entry": [], "for": resource}

    async def no_cache(*a, **k):
        return None

    monkeypatch.setattr(gw.fhir, "read", fake_read)
    monkeypatch.setattr(gw.fhir, "search", fake_search)
    monkeypatch.setattr(gw, "get_cached", no_cache)
    monkeypatch.setattr(gw, "set_cached", no_cache)

    with TestClient(gw.app, raise_server_exceptions=False) as client:
        yield client, calls


def auth(*roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token('u1', roles=list(roles))}"}


class TestAllergyRoutes:
    def test_search_reaches_the_right_resource_type(self, gateway):
        client, calls = gateway
        r = client.get(
            "/fhir/allergyintolerance/search",
            params={"patient": "MRN-1"},
            headers=auth("clinician"),
        )
        assert r.status_code == 200
        assert calls["search"][0][0] == "AllergyIntolerance"
        assert calls["search"][0][1]["patient"] == "MRN-1"

    def test_read_by_id(self, gateway):
        client, calls = gateway
        r = client.get("/fhir/allergyintolerance/a1", headers=auth("clinician"))
        assert r.status_code == 200
        assert calls["read"] == [("AllergyIntolerance", "a1")]

    def test_search_is_not_swallowed_by_the_id_route(self, gateway):
        """Route ordering: /search must not be read as an allergy id."""
        client, calls = gateway
        client.get("/fhir/allergyintolerance/search", headers=auth("clinician"))
        assert calls["read"] == []
        assert calls["search"]

    def test_requires_authentication(self, gateway):
        client, _ = gateway
        assert client.get("/fhir/allergyintolerance/search").status_code == 401

    def test_viewer_role_is_refused(self, gateway):
        client, calls = gateway
        r = client.get("/fhir/allergyintolerance/search", headers=auth("viewer"))
        assert r.status_code == 403
        assert calls["search"] == []

    def test_malformed_id_is_rejected_before_the_upstream_call(self, gateway):
        client, calls = gateway
        r = client.get("/fhir/allergyintolerance/not a valid id", headers=auth("clinician"))
        assert r.status_code in (400, 404)
        assert calls["read"] == []


class TestConditionRoutes:
    def test_search_reaches_the_right_resource_type(self, gateway):
        client, calls = gateway
        r = client.get(
            "/fhir/condition/search",
            params={"patient": "MRN-1"},
            headers=auth("clinician"),
        )
        assert r.status_code == 200
        assert calls["search"][0][0] == "Condition"

    def test_read_by_id(self, gateway):
        client, calls = gateway
        r = client.get("/fhir/condition/c1", headers=auth("clinician"))
        assert r.status_code == 200
        assert calls["read"] == [("Condition", "c1")]

    def test_clinical_status_filter_is_allowed(self, gateway):
        """Filtering to the active problem list is the common case."""
        client, calls = gateway
        r = client.get(
            "/fhir/condition/search",
            params={"patient": "MRN-1", "clinical-status": "active"},
            headers=auth("clinician"),
        )
        assert r.status_code == 200
        assert calls["search"][0][1]["clinical-status"] == "active"

    def test_unknown_search_parameters_are_still_rejected(self, gateway):
        client, calls = gateway
        r = client.get(
            "/fhir/condition/search",
            params={"patient": "MRN-1", "evil": "1"},
            headers=auth("clinician"),
        )
        assert r.status_code == 400
        assert calls["search"] == []


class TestCaching:
    def test_allergies_are_cached_more_briefly_than_reference_data(self):
        """A stale allergy list is a patient-safety problem, not just staleness."""
        import backend.services.gateway.main as gw

        assert gw.CACHE_TTL["AllergyIntolerance"] < gw.CACHE_TTL["Patient"]

    def test_new_resources_are_known_to_cache_invalidation(self):
        import backend.services.gateway.main as gw

        assert "AllergyIntolerance" in gw.KNOWN_RESOURCES
        assert "Condition" in gw.KNOWN_RESOURCES

    def test_admin_can_invalidate_the_allergy_cache(self, gateway, monkeypatch):
        import backend.services.gateway.main as gw

        seen: list[tuple[str, str | None]] = []

        async def fake_invalidate(resource, patient_id=None):
            seen.append((resource, patient_id))
            return 1

        monkeypatch.setattr(gw, "invalidate_cache", fake_invalidate)
        client, _ = gateway
        r = client.delete(
            "/cache/allergyintolerance",
            params={"patient": "MRN-1"},
            headers=auth("admin"),
        )
        assert r.status_code == 200
        assert seen == [("AllergyIntolerance", "MRN-1")]
