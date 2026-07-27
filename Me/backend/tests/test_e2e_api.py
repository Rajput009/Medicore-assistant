"""End-to-end API tests.

These drive the real FastAPI applications over HTTP (via ASGI transport),
exercising the full middleware stack: auth enforcement, RBAC, audit logging,
error mapping and caching. Only the true external dependencies (the upstream
FHIR server, Postgres, Mongo) are faked.

Together with the Playwright browser suite in ``frontend/web/e2e`` these cover
the two ends of the system.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.common.security import create_access_token

# ---------------------------------------------------------------------------
# Fakes for external systems
# ---------------------------------------------------------------------------


class FakeFhirServer:
    """In-memory stand-in for the upstream FHIR API."""

    def __init__(self) -> None:
        self.patients: dict[str, dict[str, Any]] = {
            "123": {"resourceType": "Patient", "id": "123", "active": True},
        }
        self.read_calls: list[tuple[str, str]] = []
        self.search_calls: list[tuple[str, dict[str, str]]] = []
        self.fail_with: Exception | None = None

    async def read(self, resource: str, resource_id: str) -> dict[str, Any]:
        self.read_calls.append((resource, resource_id))
        if self.fail_with:
            raise self.fail_with
        from backend.common.fhir_client import FHIRError

        if resource == "Patient" and resource_id not in self.patients:
            raise FHIRError("not found", status_code=404)
        return self.patients.get(
            resource_id, {"resourceType": resource, "id": resource_id}
        )

    async def search(self, resource: str, params: dict[str, str] | None = None):
        self.search_calls.append((resource, dict(params or {})))
        if self.fail_with:
            raise self.fail_with
        return {
            "resourceType": "Bundle",
            "total": 1,
            "entry": [{"resource": {"resourceType": resource, "id": "123"}}],
        }


class FakeCache:
    """Dict-backed replacement for the Postgres cache layer."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, tuple], tuple[float, dict]] = {}
        self.invalidations: list[tuple[str, str | None]] = []

    async def get_cached(self, resource, params, max_age_seconds=300):
        key = (resource, tuple(sorted((params or {}).items())))
        hit = self.store.get(key)
        if not hit:
            return None
        stored_at, value = hit
        if time.time() - stored_at > max_age_seconds:
            return None
        return value

    async def set_cached(self, resource, params, response):
        key = (resource, tuple(sorted((params or {}).items())))
        self.store[key] = (time.time(), response)

    async def invalidate_cache(self, resource, patient_id=None):
        self.invalidations.append((resource, patient_id))
        removed = 0
        for key in list(self.store):
            if key[0] != resource:
                continue
            if patient_id and ("patient", patient_id) not in key[1]:
                continue
            del self.store[key]
            removed += 1
        return removed


@pytest.fixture()
def gateway(monkeypatch):
    """Gateway app wired to the fakes above."""
    import backend.services.gateway.main as gw

    fhir = FakeFhirServer()
    cache = FakeCache()

    monkeypatch.setattr(gw.fhir, "read", fhir.read)
    monkeypatch.setattr(gw.fhir, "search", fhir.search)
    monkeypatch.setattr(gw, "get_cached", cache.get_cached)
    monkeypatch.setattr(gw, "set_cached", cache.set_cached)
    monkeypatch.setattr(gw, "invalidate_cache", cache.invalidate_cache)

    with TestClient(gw.app, raise_server_exceptions=False) as client:
        yield client, fhir, cache


def bearer(*roles: str, sub: str = "e2e.user", minutes: int = 60) -> dict[str, str]:
    token = create_access_token(sub=sub, roles=list(roles), expires_minutes=minutes)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth journey
# ---------------------------------------------------------------------------


class TestAuthJourney:
    def test_health_is_reachable_without_a_token(self, gateway):
        client, _, _ = gateway
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["service"] == "gateway"

    def test_protected_route_rejects_anonymous_access(self, gateway):
        client, fhir, _ = gateway
        r = client.get("/fhir/patient/123")
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        # The upstream must never be contacted for an unauthenticated caller.
        assert fhir.read_calls == []

    def test_full_login_to_resource_flow(self, gateway):
        """Mint a token the way /login does, then use it against the gateway."""
        client, fhir, _ = gateway
        r = client.get("/fhir/patient/123", headers=bearer("clinician"))
        assert r.status_code == 200
        assert r.json()["id"] == "123"
        assert fhir.read_calls == [("Patient", "123")]

    def test_expired_token_is_rejected(self, gateway):
        client, _, _ = gateway
        r = client.get("/fhir/patient/123", headers=bearer("clinician", minutes=-5))
        assert r.status_code == 401

    @pytest.mark.parametrize(
        "header",
        [
            "",
            "Bearer",
            "Bearer ",
            "Basic dXNlcjpwYXNz",
            "bearer not.a.jwt",
            "Token abc",
        ],
    )
    def test_malformed_authorization_headers_are_rejected(self, gateway, header):
        client, _, _ = gateway
        r = client.get("/fhir/patient/123", headers={"Authorization": header})
        assert r.status_code == 401

    def test_bearer_scheme_is_case_insensitive(self, gateway):
        client, _, _ = gateway
        token = create_access_token(sub="u", roles=["clinician"])
        r = client.get("/fhir/patient/123", headers={"Authorization": f"bEaReR {token}"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class TestRbac:
    @pytest.mark.parametrize(
        "path",
        [
            "/fhir/patient/123",
            "/fhir/patient/search",
            "/fhir/encounter/search",
            "/fhir/observation/search",
            "/fhir/medicationrequest/search",
        ],
    )
    def test_viewer_is_forbidden_from_clinical_data(self, gateway, path):
        client, fhir, _ = gateway
        r = client.get(path, headers=bearer("viewer"))
        assert r.status_code == 403
        assert fhir.read_calls == [] and fhir.search_calls == []

    def test_clinician_allowed_admin_allowed(self, gateway):
        client, _, _ = gateway
        assert client.get("/fhir/patient/123", headers=bearer("clinician")).status_code == 200
        assert client.get("/fhir/patient/123", headers=bearer("admin")).status_code == 200

    def test_cache_invalidation_is_admin_only(self, gateway):
        client, _, cache = gateway
        assert client.delete("/cache/Patient", headers=bearer("clinician")).status_code == 403
        assert cache.invalidations == []

        r = client.delete("/cache/Patient", headers=bearer("admin"))
        assert r.status_code == 200
        assert cache.invalidations == [("Patient", None)]

    def test_token_with_no_roles_gets_403_not_500(self, gateway):
        client, _, _ = gateway
        r = client.get("/fhir/patient/123", headers=bearer())
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# FHIR proxy behaviour
# ---------------------------------------------------------------------------


class TestFhirProxy:
    def test_search_is_not_shadowed_by_the_id_route(self, gateway):
        """`/search` must resolve to a search, never to a read of id="search"."""
        client, fhir, _ = gateway
        r = client.get("/fhir/patient/search?patient=123", headers=bearer("clinician"))
        assert r.status_code == 200
        assert r.json()["resourceType"] == "Bundle"
        assert len(fhir.search_calls) == 1
        resource, params = fhir.search_calls[0]
        assert resource == "Patient"
        assert params["patient"] == "123"
        assert fhir.read_calls == []

    def test_query_parameters_are_forwarded(self, gateway):
        client, fhir, _ = gateway
        client.get(
            "/fhir/observation/search?patient=123&code=789-8",
            headers=bearer("clinician"),
        )
        resource, params = fhir.search_calls[-1]
        assert resource == "Observation"
        assert params["patient"] == "123"
        assert params["code"] == "789-8"

    def test_upstream_404_is_preserved(self, gateway):
        client, _, _ = gateway
        r = client.get("/fhir/patient/does-not-exist", headers=bearer("clinician"))
        assert r.status_code == 404

    def test_upstream_failure_becomes_502(self, gateway):
        client, fhir, _ = gateway
        from backend.common.fhir_client import FHIRError

        fhir.fail_with = FHIRError("connection refused")
        r = client.get("/fhir/patient/123", headers=bearer("clinician"))
        assert r.status_code == 502

    def test_ids_with_special_characters_are_rejected(self, gateway):
        """FHIR ids are constrained by the spec; anything else is malformed
        input and must not be interpolated into the upstream URL."""
        client, fhir, _ = gateway
        r = client.get("/fhir/patient/abc%20def", headers=bearer("clinician"))
        assert r.status_code == 400
        assert fhir.read_calls == []

    def test_valid_ids_are_accepted(self, gateway):
        client, _, _ = gateway
        for good in ("123", "MRN-000123", "abc.def", "a_b" if False else "A-1"):
            assert client.get(f"/fhir/patient/{good}", headers=bearer("clinician")).status_code in (200, 404)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    def test_second_identical_search_is_served_from_cache(self, gateway):
        client, fhir, _ = gateway
        h = bearer("clinician")
        first = client.get("/fhir/patient/search?patient=123", headers=h)
        second = client.get("/fhir/patient/search?patient=123", headers=h)

        assert first.json() == second.json()
        # Only the first request reached the upstream server.
        assert len(fhir.search_calls) == 1

    def test_different_parameters_are_cached_separately(self, gateway):
        client, fhir, _ = gateway
        h = bearer("clinician")
        client.get("/fhir/patient/search?patient=1", headers=h)
        client.get("/fhir/patient/search?patient=2", headers=h)
        assert len(fhir.search_calls) == 2

    def test_parameter_order_does_not_create_a_duplicate_entry(self, gateway):
        client, fhir, _ = gateway
        h = bearer("clinician")
        client.get("/fhir/observation/search?patient=1&code=x", headers=h)
        client.get("/fhir/observation/search?code=x&patient=1", headers=h)
        assert len(fhir.search_calls) == 1

    def test_invalidation_forces_a_refetch(self, gateway):
        client, fhir, _ = gateway
        h = bearer("clinician")
        client.get("/fhir/patient/search?patient=123", headers=h)
        assert len(fhir.search_calls) == 1

        client.delete("/cache/Patient", headers=bearer("admin"))
        client.get("/fhir/patient/search?patient=123", headers=h)
        assert len(fhir.search_calls) == 2

    def test_patient_scoped_invalidation_leaves_other_patients_cached(self, gateway):
        client, fhir, _ = gateway
        h = bearer("clinician")
        client.get("/fhir/patient/search?patient=1", headers=h)
        client.get("/fhir/patient/search?patient=2", headers=h)
        assert len(fhir.search_calls) == 2

        client.delete("/cache/Patient?patient=1", headers=bearer("admin"))
        client.get("/fhir/patient/search?patient=2", headers=h)  # still cached
        assert len(fhir.search_calls) == 2
        client.get("/fhir/patient/search?patient=1", headers=h)  # refetched
        assert len(fhir.search_calls) == 3

    def test_unknown_resource_is_rejected(self, gateway):
        client, _, _ = gateway
        r = client.delete("/cache/NotAResource", headers=bearer("admin"))
        assert r.status_code == 400

    def test_resource_name_is_case_insensitive(self, gateway):
        client, _, cache = gateway
        r = client.delete("/cache/patient", headers=bearer("admin"))
        assert r.status_code == 200
        assert cache.invalidations == [("Patient", None)]

    def test_cache_outage_does_not_break_reads(self, gateway, monkeypatch):
        """A failing cache must degrade to a direct upstream fetch, not a 500."""
        import backend.services.gateway.main as gw

        async def boom(*_a, **_k):
            raise RuntimeError("postgres is down")

        monkeypatch.setattr(gw, "get_cached", boom)
        monkeypatch.setattr(gw, "set_cached", boom)

        client, _, _ = gateway
        r = client.get("/fhir/patient/search?patient=123", headers=bearer("clinician"))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Other services
# ---------------------------------------------------------------------------


class TestCdsService:
    @pytest.fixture()
    def client(self):
        import backend.services.cds.main as m

        return TestClient(m.app)

    def test_end_to_end_scoring(self, client):
        r = client.post("/risk", json={"hr": 72, "sbp": 120, "spo2": 98}, headers=bearer("clinician"))
        assert r.status_code == 200
        body = r.json()
        assert body["class_label"] == "low"
        assert 0.0 <= body["score"] <= 1.0

    def test_critical_patient_is_high_risk(self, client):
        r = client.post("/risk", json={"hr": 190, "sbp": 55, "spo2": 78}, headers=bearer("clinician"))
        assert r.json()["class_label"] == "high"

    @pytest.mark.parametrize(
        "payload",
        [
            {"hr": 72, "sbp": 120},  # missing field
            {"hr": "fast", "sbp": 120, "spo2": 98},  # wrong type
            {"hr": 72, "sbp": 120, "spo2": 101},  # out of range
            {"hr": 0, "sbp": 120, "spo2": 98},  # non-positive
        ],
    )
    def test_invalid_payloads_are_rejected(self, client, payload):
        r = client.post("/risk", json=payload, headers=bearer("clinician"))
        assert r.status_code == 422

    def test_health(self, client):
        assert client.get("/health").json()["service"] == "cds"


class TestAuthService:
    @pytest.fixture()
    def client(self, monkeypatch):
        monkeypatch.setenv("ENV", "test")
        import backend.services.auth.main as m

        return TestClient(m.app)

    def test_login_issues_a_usable_token(self, client, monkeypatch):
        import backend.services.auth.main as m

        monkeypatch.setattr(m, "_demo_login_enabled", lambda: True)
        monkeypatch.setenv("DEMO_PASSWORD", "medicore-dev")

        r = client.post(
            "/login", json={"username": "dr.smith", "password": "medicore-dev"}
        )
        assert r.status_code == 200
        token = r.json()["access_token"]

        # The token the auth service mints must satisfy the gateway.
        from backend.common.security import verify_access_token

        claims = verify_access_token(token)
        assert claims["sub"] == "dr.smith"
        assert "clinician" in claims["roles"]

    def test_wrong_password_is_rejected(self, client, monkeypatch):
        import backend.services.auth.main as m

        monkeypatch.setattr(m, "_demo_login_enabled", lambda: True)
        r = client.post("/login", json={"username": "u", "password": "nope"})
        assert r.status_code == 401

    def test_oidc_endpoints_report_when_unconfigured(self, client):
        assert client.get("/oidc/login").status_code == 501


# ---------------------------------------------------------------------------
# Cross-service contract
# ---------------------------------------------------------------------------


def test_auth_token_is_accepted_by_the_gateway(gateway, monkeypatch):
    """The critical integration point: auth mints, gateway verifies."""
    monkeypatch.setenv("ENV", "test")
    import backend.services.auth.main as auth_main

    monkeypatch.setattr(auth_main, "_demo_login_enabled", lambda: True)
    monkeypatch.setenv("DEMO_PASSWORD", "medicore-dev")

    auth_client = TestClient(auth_main.app)
    token = auth_client.post(
        "/login", json={"username": "dr.smith", "password": "medicore-dev"}
    ).json()["access_token"]

    gateway_client, _, _ = gateway
    r = gateway_client.get(
        "/fhir/patient/123", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
