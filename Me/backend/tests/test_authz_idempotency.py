"""Patient/ward scope, idempotency keys, and production-safe errors."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from backend.common.deps import Principal
from backend.common.errors import public_detail
from backend.common.idempotency import reset_idempotency_store
from backend.common.security import create_access_token
from backend.tests.fakes import FakePatientFlowRepository


@pytest.fixture(autouse=True)
def _clean_idem():
    reset_idempotency_store()
    yield
    reset_idempotency_store()


class TestPrincipalScope:
    def test_empty_wards_means_unrestricted(self):
        p = Principal(sub="u", roles=["clinician"], wards=[])
        assert p.can_access_ward("ICU") is True

    def test_ward_scope_enforced(self):
        p = Principal(sub="u", roles=["clinician"], wards=["A"])
        assert p.can_access_ward("A") is True
        assert p.can_access_ward("ICU") is False

    def test_admin_bypasses_ward_scope(self):
        p = Principal(sub="a", roles=["admin"], wards=["A"])
        assert p.can_access_ward("ICU") is True

    def test_department_scope(self):
        p = Principal(sub="u", roles=["clinician"], departments=["ED"])
        assert p.can_access_department("ED") is True
        assert p.can_access_department("ICU") is False

    def test_patient_assigned_set(self):
        p = Principal(sub="u", roles=["clinician"])
        assert p.can_access_patient("P1", assigned={"P1", "P2"}) is True
        assert p.can_access_patient("P9", assigned={"P1"}) is False
        assert p.can_access_patient("P9", assigned=None) is True

    def test_wards_parsed_from_token_claims(self):

        token = create_access_token("u", roles=["clinician"])
        # Rebuild token with wards claim.
        from datetime import UTC, datetime, timedelta

        from jose import jwt

        from backend.common.config import settings

        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "sub": "scoped",
                "roles": ["clinician"],
                "wards": ["A", "B"],
                "departments": ["ED"],
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=15)).timestamp()),
                "jti": "scope-jti",
                "token_use": "access",
            },
            settings.jwt_secret,
            algorithm="HS256",
        )
        # Driven through a real request rather than a hand-built ASGI scope:
        # get_principal reads the Authorization header and writes to
        # request.state, so the dependency-injection path is the thing worth
        # exercising.
        from fastapi import Depends

        from backend.common import deps

        app = FastAPI()

        @app.get("/me")
        def me(principal: Principal = Depends(deps.get_principal)):
            return {
                "sub": principal.sub,
                "wards": principal.wards,
                "departments": principal.departments,
            }

        c = TestClient(app)
        r = c.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["wards"] == ["A", "B"]
        assert r.json()["departments"] == ["ED"]


class TestIdempotencyFlow:
    @pytest.fixture()
    def flow(self):
        import backend.services.patient_flow.main as pf

        repo = FakePatientFlowRepository(
            beds=[{"bed_id": "A-001", "ward": "A"}, {"bed_id": "ICU-001", "ward": "ICU"}]
        )
        pf.app.dependency_overrides[pf.get_repository] = lambda: repo
        pf._repository = repo
        try:
            with TestClient(pf.app, raise_server_exceptions=False) as client:
                yield client, repo
        finally:
            pf.app.dependency_overrides.clear()
            pf._repository = None

    def _auth(self, *roles: str, **claims_extra) -> dict[str, str]:
        from datetime import UTC, datetime, timedelta

        from jose import jwt

        from backend.common.config import settings

        now = datetime.now(UTC)
        payload = {
            "sub": "u1",
            "roles": list(roles) or ["clinician"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "jti": f"jti-{roles}-{claims_extra}",
            "token_use": "access",
            **claims_extra,
        }
        token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    def test_enqueue_idempotent_retry(self, flow):
        client, repo = flow
        headers = {
            **self._auth("clinician"),
            "Idempotency-Key": "enq-1",
            "X-Forwarded-For": "198.51.100.50",
        }
        r1 = client.post(
            "/queue",
            json={"patient_id": "MRN-1", "acuity": 2, "dept": "ED"},
            headers=headers,
        )
        assert r1.status_code == 201
        assert len(repo.queue_store) == 1

        r2 = client.post(
            "/queue",
            json={"patient_id": "MRN-1", "acuity": 2, "dept": "ED"},
            headers=headers,
        )
        assert r2.status_code == 201
        assert r2.headers.get("idempotent-replayed") == "true"
        # Must not double-insert.
        assert len(repo.queue_store) == 1

    def test_ward_scope_filters_beds(self, flow):
        client, _ = flow
        headers = {
            **self._auth("clinician", wards=["A"]),
            "X-Forwarded-For": "198.51.100.51",
        }
        r = client.get("/beds", headers=headers)
        assert r.status_code == 200
        wards = {b["ward"] for b in r.json()}
        assert wards == {"A"}

    def test_ward_scope_blocks_other_ward_update(self, flow):
        client, _ = flow
        headers = {
            **self._auth("clinician", wards=["A"]),
            "X-Forwarded-For": "198.51.100.52",
        }
        r = client.patch(
            "/beds/ICU-001",
            json={"occupied": True, "patient_id": "MRN-9"},
            headers=headers,
        )
        assert r.status_code == 403

    def test_department_scope_blocks_enqueue(self, flow):
        client, _ = flow
        headers = {
            **self._auth("clinician", departments=["ED"]),
            "X-Forwarded-For": "198.51.100.53",
        }
        r = client.post(
            "/queue",
            json={"patient_id": "MRN-2", "acuity": 1, "dept": "OR"},
            headers=headers,
        )
        assert r.status_code == 403


class TestProductionSafeErrors:
    def test_public_detail_scrubs_5xx_in_production(self, monkeypatch):
        from backend.common import config

        monkeypatch.setattr(config.settings, "env", "production")
        assert public_detail(500, preferred="Secret DSN leaked") == (
            "An unexpected error occurred"
        )
        assert public_detail(502, preferred="upstream boom") == (
            "Upstream service unavailable"
        )

    def test_public_detail_keeps_safe_4xx_messages(self, monkeypatch):
        from backend.common import config

        monkeypatch.setattr(config.settings, "env", "production")
        msg = public_detail(403, preferred="Not authorised for this ward")
        assert msg == "Not authorised for this ward"

    def test_unhandled_exception_does_not_echo_message(self):
        from backend.common.app import create_service_app

        app = create_service_app(title="t", service_name="t", version="0")

        @app.get("/boom")
        def boom():
            raise RuntimeError("postgres://user:password@host/db")

        c = TestClient(app, raise_server_exceptions=False)
        r = c.get("/boom")
        assert r.status_code == 500
        assert "password" not in r.text
        assert "postgres" not in r.text.lower()
