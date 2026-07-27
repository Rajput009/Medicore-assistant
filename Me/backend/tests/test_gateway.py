"""Gateway routing, auth enforcement and RBAC tests."""

from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.common.security import create_access_token


@pytest.fixture()
def gateway(monkeypatch):
    import backend.services.gateway.main as gw

    calls: dict[str, Any] = {"read": [], "search": [], "invalidated": []}

    async def fake_read(resource, resource_id):
        calls["read"].append((resource, resource_id))
        return {"resourceType": resource, "id": resource_id}

    async def fake_search(resource, params=None):
        calls["search"].append((resource, dict(params or {})))
        return {"resourceType": "Bundle", "entry": [], "for": resource}

    monkeypatch.setattr(gw.fhir, "read", fake_read)
    monkeypatch.setattr(gw.fhir, "search", fake_search)

    # Bypass Postgres entirely.
    async def no_cache(*a, **k):
        return None

    async def noop_set(*a, **k):
        return None

    async def fake_invalidate(resource, patient_id=None):
        calls["invalidated"].append((resource, patient_id))
        return 3

    monkeypatch.setattr(gw, "get_cached", no_cache)
    monkeypatch.setattr(gw, "set_cached", noop_set)
    monkeypatch.setattr(gw, "invalidate_cache", fake_invalidate)

    with TestClient(gw.app, raise_server_exceptions=False) as client:
        yield client, calls


def auth(roles):
    return {"Authorization": f"Bearer {create_access_token('u1', roles=roles)}"}


# --- public routes -------------------------------------------------------


def test_health_is_public(gateway):
    client, _ = gateway
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "gateway"


def test_ready_is_public_for_probes(gateway):
    """Kubernetes readiness and Docker HEALTHCHECK send no Authorization."""
    client, _ = gateway
    # May be 200 or 503 depending on whether the cache pool is up; never 401.
    assert client.get("/ready").status_code in (200, 503)


def test_openapi_docs_are_not_public_by_default(gateway):
    """The schema is a free reconnaissance map; keep it off the public surface."""
    client, _ = gateway
    for path in ("/docs", "/redoc", "/openapi.json"):
        # Either 401 (auth required) or 404 (route disabled). Never 200.
        assert client.get(path).status_code in (401, 404)


# --- authentication ------------------------------------------------------


def test_missing_token_returns_401_not_500(gateway):
    """Regression: HTTPException raised in BaseHTTPMiddleware surfaced as a 500."""
    client, _ = gateway
    r = client.get("/fhir/patient/123")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_invalid_token_returns_401(gateway):
    client, _ = gateway
    r = client.get("/fhir/patient/123", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


def test_malformed_scheme_returns_401(gateway):
    client, _ = gateway
    r = client.get("/fhir/patient/123", headers={"Authorization": "Basic abc"})
    assert r.status_code == 401


def test_exempt_path_is_exact_match(gateway):
    """Regression: prefix matching let '/health-secrets' bypass auth."""
    client, _ = gateway
    assert client.get("/healthzzz-admin").status_code in (401, 404)


# --- RBAC ----------------------------------------------------------------


def test_viewer_forbidden(gateway):
    client, _ = gateway
    r = client.get("/fhir/patient/123", headers=auth(["viewer"]))
    assert r.status_code == 403


def test_clinician_allowed(gateway):
    client, calls = gateway
    r = client.get("/fhir/patient/123", headers=auth(["clinician"]))
    assert r.status_code == 200
    assert calls["read"] == [("Patient", "123")]


def test_unprotected_patient_route_is_now_protected(gateway):
    """Regression: GET /fhir/patient/{id} previously had no auth at all."""
    client, _ = gateway
    assert client.get("/fhir/patient/999").status_code == 401


# --- routing -------------------------------------------------------------


@pytest.mark.parametrize(
    "path,resource",
    [
        ("/fhir/patient/search", "Patient"),
        ("/fhir/encounter/search", "Encounter"),
        ("/fhir/observation/search", "Observation"),
        ("/fhir/medicationrequest/search", "MedicationRequest"),
    ],
)
def test_search_routes_not_shadowed_by_id_routes(gateway, path, resource):
    """Regression: '/{id}' was declared first and swallowed '/search'."""
    client, calls = gateway
    r = client.get(f"{path}?patient=123", headers=auth(["clinician"]))
    assert r.status_code == 200
    assert r.json()["resourceType"] == "Bundle"
    # The gateway injects a bounded _count so a caller cannot pull an
    # unbounded page of PHI.
    resource_called, params = calls["search"][-1]
    assert resource_called == resource
    assert params["patient"] == "123"
    assert int(params["_count"]) <= 100
    assert calls["read"] == []  # must not have been treated as an id


# --- cache invalidation --------------------------------------------------


def test_cache_invalidation_requires_admin(gateway):
    client, _ = gateway
    assert client.delete("/cache/Patient", headers=auth(["clinician"])).status_code == 403


def test_admin_can_invalidate(gateway):
    client, calls = gateway
    r = client.delete("/cache/Patient?patient=42", headers=auth(["admin"]))
    assert r.status_code == 200
    assert r.json()["deleted"] == 3
    assert calls["invalidated"] == [("Patient", "42")]


def test_unknown_resource_rejected(gateway):
    client, _ = gateway
    r = client.delete("/cache/DROP-TABLE", headers=auth(["admin"]))
    assert r.status_code == 400
