"""Ward / department scope: minting, mapping and enforcement.

Regression suite for a hole where the enforcement code in
``Principal.can_access_ward`` was correct but *nothing ever populated the
claim*: ``create_access_token`` had no wards argument and the OIDC callback
never mapped IdP groups. Every SSO user therefore fell through the
"empty scope = unrestricted" branch, silently disabling ward scoping.
"""

from __future__ import annotations

import importlib

import pytest
from starlette.testclient import TestClient

from backend.common.deps import Principal
from backend.common.security import create_access_token, verify_access_token
from backend.tests.fakes import FakePatientFlowRepository


class TestTokenScopeClaims:
    def test_wards_are_embedded_in_the_token(self):
        token = create_access_token("u1", roles=["clinician"], wards=["ICU", "A"])
        assert verify_access_token(token)["wards"] == ["ICU", "A"]

    def test_departments_are_embedded_in_the_token(self):
        token = create_access_token("u1", roles=["clinician"], departments=["ED"])
        assert verify_access_token(token)["departments"] == ["ED"]

    def test_empty_scope_omits_the_claim_entirely(self):
        """An empty list must not be emitted.

        ``[]`` and "claim absent" mean opposite things to a naive reader, and
        only the absent form is treated as unrestricted. Emitting ``[]`` would
        risk a future change reading it as "access to zero wards".
        """
        payload = verify_access_token(create_access_token("u1", roles=["clinician"]))
        assert "wards" not in payload
        assert "departments" not in payload

    def test_scope_entries_are_deduplicated_and_trimmed(self):
        token = create_access_token("u1", wards=["  ICU ", "ICU", "A", ""])
        assert verify_access_token(token)["wards"] == ["ICU", "A"]

    def test_absurdly_long_scope_entries_are_dropped(self):
        """Scope values are compared to ward ids and land in audit records."""
        token = create_access_token("u1", wards=["A", "x" * 200])
        assert verify_access_token(token)["wards"] == ["A"]

    def test_scope_survives_the_principal_round_trip(self):
        payload = verify_access_token(
            create_access_token("u1", roles=["clinician"], wards=["ICU"])
        )
        principal = Principal(
            sub=payload["sub"], roles=payload["roles"], wards=payload["wards"]
        )
        assert principal.can_access_ward("ICU") is True
        assert principal.can_access_ward("A") is False


class TestOidcGroupMapping:
    """Hospital IdPs express scope as group membership."""

    @pytest.fixture()
    def auth_mod(self):
        return importlib.import_module("backend.services.auth.main")

    def test_ward_groups_map_to_wards(self, auth_mod):
        wards, depts = auth_mod._map_scopes(
            {"groups": ["medicore-clinician", "medicore-ward-ICU", "medicore-ward-A"]}
        )
        assert wards == ["ICU", "A"]
        assert depts == []

    def test_department_groups_map_to_departments(self, auth_mod):
        wards, depts = auth_mod._map_scopes({"groups": ["medicore-dept-ED"]})
        assert depts == ["ED"]
        assert wards == []

    def test_unrelated_groups_are_ignored(self, auth_mod):
        wards, depts = auth_mod._map_scopes(
            {"groups": ["vpn-users", "medicore-admin", "domain-users"]}
        )
        assert (wards, depts) == ([], [])

    def test_direct_claims_are_accepted(self, auth_mod):
        """Some IdPs can emit dedicated claims instead of groups."""
        wards, depts = auth_mod._map_scopes({"wards": ["B"], "departments": "ED,ICU"})
        assert wards == ["B"]
        assert depts == ["ED", "ICU"]

    def test_group_and_direct_claims_merge_without_duplicates(self, auth_mod):
        wards, _ = auth_mod._map_scopes(
            {"groups": ["medicore-ward-ICU"], "wards": ["ICU", "A"]}
        )
        assert wards == ["ICU", "A"]

    def test_no_scope_claims_yields_unrestricted(self, auth_mod):
        """Back-compat: an IdP publishing no scope must not lock users out."""
        assert auth_mod._map_scopes({"sub": "abc"}) == ([], [])

    def test_roles_still_default_to_least_privilege(self, auth_mod):
        assert auth_mod._map_roles({"groups": ["medicore-ward-ICU"]}) == ["viewer"]


class TestScopeIsEnforcedEndToEnd:
    """The claim must actually restrict data, not just ride along."""

    @pytest.fixture()
    def flow(self):
        import backend.services.patient_flow.main as pf

        repo = FakePatientFlowRepository(
            beds=[
                {"bed_id": "A-001", "ward": "A"},
                {"bed_id": "ICU-001", "ward": "ICU"},
            ]
        )
        pf.app.dependency_overrides[pf.get_repository] = lambda: repo
        pf._repository = repo
        try:
            with TestClient(pf.app, raise_server_exceptions=False) as client:
                yield client
        finally:
            pf.app.dependency_overrides.clear()
            pf._repository = None

    def _headers(self, **kwargs) -> dict[str, str]:
        token = create_access_token("u1", roles=["clinician"], **kwargs)
        return {"Authorization": f"Bearer {token}"}

    def test_scoped_token_sees_only_its_ward(self, flow):
        r = flow.get("/beds", headers=self._headers(wards=["ICU"]))
        assert r.status_code == 200
        assert {b["ward"] for b in r.json()} == {"ICU"}

    def test_unscoped_token_still_sees_every_ward(self, flow):
        r = flow.get("/beds", headers=self._headers())
        assert r.status_code == 200
        # Startup also seeds BED_LAYOUT, so assert a superset rather than an
        # exact match: the point is that no ward filtering happened.
        wards = {b["ward"] for b in r.json()}
        assert {"A", "ICU"} <= wards
        assert len(wards) > 1

    def test_requesting_another_ward_explicitly_is_forbidden(self, flow):
        r = flow.get("/beds", params={"ward": "A"}, headers=self._headers(wards=["ICU"]))
        assert r.status_code == 403

    def test_enqueue_outside_department_scope_is_forbidden(self, flow):
        r = flow.post(
            "/queue",
            json={"patient_id": "MRN-1", "acuity": 2, "dept": "ICU", "reason": "Deteriorating observations requiring urgent review"},
            headers=self._headers(departments=["ED"]),
        )
        assert r.status_code == 403


class TestSessionExposesScope:
    """The SPA filters its own UI, so /session must report the scope."""

    @pytest.fixture()
    def auth_client(self):
        import backend.services.auth.main as auth_main

        with TestClient(auth_main.app, raise_server_exceptions=False) as client:
            yield client

    def test_session_returns_wards_and_departments(self, auth_client):
        token = create_access_token(
            "dr.smith", roles=["clinician"], wards=["ICU"], departments=["ED"]
        )
        r = auth_client.get("/session", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["wards"] == ["ICU"]
        assert r.json()["departments"] == ["ED"]

    def test_session_reports_empty_scope_for_unscoped_tokens(self, auth_client):
        token = create_access_token("dr.smith", roles=["clinician"])
        r = auth_client.get("/session", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["wards"] == []
        assert r.json()["departments"] == []

    def test_session_never_echoes_the_raw_token(self, auth_client):
        token = create_access_token("dr.smith", roles=["clinician"], wards=["ICU"])
        r = auth_client.get("/session", headers={"Authorization": f"Bearer {token}"})
        assert token not in r.text
