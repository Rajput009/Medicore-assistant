"""Authentication and RBAC on the internal services.

patient_flow and cds are reachable directly inside the cluster, so they must
enforce auth themselves rather than relying on the gateway sitting in front.
These are regression tests for a hole where /queue and /beds served patient
identifiers to completely unauthenticated callers.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.common.deps import normalise_roles
from backend.common.security import create_access_token
from backend.tests.fakes import FakePatientFlowRepository


def auth(*roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token('u1', roles=list(roles))}"}


@pytest.fixture()
def flow():
    """Patient-flow app backed by an in-memory repository.

    The dependency is overridden rather than the module global patched, so the
    real handler, validation and error-translation code all still run.
    """
    import backend.services.patient_flow.main as pf

    repo = FakePatientFlowRepository(
        beds=[{"bed_id": "A-001", "ward": "A"}, {"bed_id": "A-002", "ward": "A"}]
    )
    repo.queue_store.append(
        {
            "patient_id": "MRN-000123",
            "acuity": 1,
            "dept": "ED",
            "status": "waiting",
            "created_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            "created_by": "seed",
        }
    )
    pf.app.dependency_overrides[pf.get_repository] = lambda: repo
    # Also set the module global so lifespan skips real database setup
    # instead of blocking on server selection.
    pf._repository = repo
    try:
        with TestClient(pf.app, raise_server_exceptions=False) as client:
            yield client, repo
    finally:
        pf.app.dependency_overrides.clear()
        pf._repository = None


@pytest.fixture()
def cds():
    import backend.services.cds.main as m

    return TestClient(m.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# patient_flow
# ---------------------------------------------------------------------------


class TestPatientFlowAuth:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/queue"),
            ("get", "/beds"),
            ("post", "/queue"),
            ("patch", "/beds/A-001"),
        ],
    )
    def test_anonymous_access_is_rejected(self, flow, method, path):
        client, queue = flow
        payload: dict = {"patient_id": "MRN-1", "acuity": 1, "dept": "ED", "reason": "Deteriorating observations requiring urgent review"}
        if method == "patch":
            payload = {"occupied": True, "patient_id": "MRN-1"}
        resp = getattr(client, method)(
            path, **({"json": payload} if method in ("post", "patch") else {})
        )
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"

    def test_no_patient_data_leaks_to_an_anonymous_caller(self, flow):
        client, repo = flow
        body = client.get("/queue").text
        assert "MRN-000123" not in body

    def test_anonymous_writes_never_reach_the_database(self, flow):
        client, repo = flow
        before = len(repo.queue_store)
        client.post("/queue", json={"patient_id": "MRN-9", "acuity": 1, "dept": "ED", "reason": "Deteriorating observations requiring urgent review"})
        assert len(repo.queue_store) == before

    def test_health_stays_public_for_probes(self, flow):
        client, _ = flow
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["service"] == "patient-flow"

    @pytest.mark.parametrize("path", ["/queue", "/beds"])
    def test_viewer_is_forbidden(self, flow, path):
        client, _ = flow
        assert client.get(path, headers=auth("viewer")).status_code == 403

    @pytest.mark.parametrize("role", ["clinician", "admin"])
    def test_clinical_staff_may_read(self, flow, role):
        client, _ = flow
        r = client.get("/queue", headers=auth(role))
        assert r.status_code == 200
        assert r.json()["items"][0]["patient_id"] == "MRN-000123"

    def test_clinician_may_enqueue(self, flow):
        client, repo = flow
        r = client.post(
            "/queue",
            json={"patient_id": "MRN-7", "acuity": 2, "dept": "ICU", "reason": "Deteriorating observations requiring urgent review"},
            headers=auth("clinician"),
        )
        assert r.status_code == 201
        assert any(i["patient_id"] == "MRN-7" for i in repo.queue_store)

    def test_expired_token_is_rejected(self, flow):
        client, _ = flow
        token = create_access_token("u", roles=["clinician"], expires_minutes=-1)
        r = client.get("/queue", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    @pytest.mark.parametrize(
        "header", ["", "Bearer", "Bearer ", "Basic abc", "Bearer not.a.jwt"]
    )
    def test_malformed_authorization_is_rejected(self, flow, header):
        client, _ = flow
        assert client.get("/queue", headers={"Authorization": header}).status_code == 401

    def test_bed_toggle_requires_clinical_role(self, flow):
        client, _ = flow
        beds = client.get("/beds", headers=auth("clinician")).json()
        bed_id = beds[0]["bed_id"]
        body = {"occupied": True, "patient_id": "MRN-5"}

        assert client.patch(f"/beds/{bed_id}", json=body, headers=auth("viewer")).status_code == 403
        assert (
            client.patch(f"/beds/{bed_id}", json=body, headers=auth("clinician")).status_code
            == 200
        )


# ---------------------------------------------------------------------------
# cds
# ---------------------------------------------------------------------------


class TestCdsAuth:
    def test_scoring_requires_authentication(self, cds):
        r = cds.post("/risk", json={"hr": 72, "sbp": 120, "spo2": 98})
        assert r.status_code == 401

    def test_viewer_is_forbidden(self, cds):
        r = cds.post(
            "/risk", json={"hr": 72, "sbp": 120, "spo2": 98}, headers=auth("viewer")
        )
        assert r.status_code == 403

    def test_clinician_may_score(self, cds):
        r = cds.post(
            "/risk", json={"hr": 72, "sbp": 120, "spo2": 98}, headers=auth("clinician")
        )
        assert r.status_code == 200
        assert r.json()["class_label"] == "low"

    def test_health_stays_public(self, cds):
        assert cds.get("/health").status_code == 200

    def test_auth_is_checked_before_payload_validation(self, cds):
        """An anonymous caller must not be able to probe the schema."""
        r = cds.post("/risk", json={"nonsense": True})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# role normalisation
# ---------------------------------------------------------------------------


class TestNormaliseRoles:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, []),
            ([], []),
            (["admin"], ["admin"]),
            ("admin", ["admin"]),
            ("admin clinician", ["admin", "clinician"]),
            ("admin,viewer", ["admin", "viewer"]),
            (["ADMIN", "Admin"], ["admin"]),
            (["admin", "superuser"], ["admin"]),
            (123, []),
            ({"admin"}, ["admin"]),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert normalise_roles(raw) == expected

    def test_unknown_roles_never_grant_access(self, flow):
        client, _ = flow
        r = client.get("/queue", headers=auth("root", "superuser"))
        assert r.status_code == 403
