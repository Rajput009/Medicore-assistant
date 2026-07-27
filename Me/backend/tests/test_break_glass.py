"""Break-glass: emergency override of ward / department scope.

A clinician responding to an arrest on a ward they are not assigned to must not
be stopped by an access-control rule. The safe answer is not a looser rule but
an explicit, justified, loudly-audited override.

The tests below pin the properties that keep this an override rather than a
backdoor: it is per-request, it demands a reason, it cannot escalate a *role*,
and it always leaves a distinct audit record.
"""

from __future__ import annotations

import itertools
import json
import logging

import pytest
from starlette.testclient import TestClient

from backend.common.security import create_access_token
from backend.tests.fakes import FakePatientFlowRepository

_ip_counter = itertools.count(1)

REASON = "Cardiac arrest in ICU-3, responding as on-call registrar"


def auth(*roles: str, **claims) -> dict[str, str]:
    token = create_access_token("dr.oncall", roles=list(roles) or ["clinician"], **claims)
    return {
        "Authorization": f"Bearer {token}",
        # Unique source IP: the in-process rate limiter buckets by client IP.
        "X-Forwarded-For": f"203.0.116.{next(_ip_counter) % 250 + 1}",
    }


@pytest.fixture()
def flow():
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
            yield client, repo
    finally:
        pf.app.dependency_overrides.clear()
        pf._repository = None


class TestScopeIsStillEnforcedWithoutIt:
    def test_out_of_scope_ward_is_refused(self, flow):
        client, _ = flow
        r = client.get("/beds", params={"ward": "ICU"}, headers=auth("clinician", wards=["A"]))
        assert r.status_code == 403

    def test_the_refusal_advertises_that_an_override_exists(self, flow):
        """A clinician in an emergency must not have to guess."""
        client, _ = flow
        r = client.get("/beds", params={"ward": "ICU"}, headers=auth("clinician", wards=["A"]))
        assert r.headers.get("X-Break-Glass-Available") == "true"

    def test_out_of_scope_department_is_refused(self, flow):
        client, _ = flow
        r = client.post(
            "/queue",
            json={"patient_id": "MRN-1", "acuity": 1, "dept": "ICU"},
            headers=auth("clinician", departments=["ED"]),
        )
        assert r.status_code == 403


class TestOverride:
    def test_a_justified_override_grants_ward_access(self, flow):
        client, _ = flow
        r = client.get(
            "/beds",
            params={"ward": "ICU"},
            headers={**auth("clinician", wards=["A"]), "X-Break-Glass-Reason": REASON},
        )
        assert r.status_code == 200
        assert {b["ward"] for b in r.json()} == {"ICU"}

    def test_a_justified_override_grants_department_access(self, flow):
        client, _ = flow
        r = client.post(
            "/queue",
            json={"patient_id": "MRN-1", "acuity": 1, "dept": "ICU"},
            headers={
                **auth("clinician", departments=["ED"]),
                "X-Break-Glass-Reason": REASON,
            },
        )
        assert r.status_code == 201

    def test_an_override_lifts_the_implicit_ward_filter_too(self, flow):
        """Scope is also applied by filtering an unfiltered list; the override
        has to reach that path as well, or it only half works."""
        client, _ = flow
        scoped = client.get("/beds", headers=auth("clinician", wards=["A"]))
        assert {b["ward"] for b in scoped.json()} == {"A"}

        opened = client.get(
            "/beds",
            headers={**auth("clinician", wards=["A"]), "X-Break-Glass-Reason": REASON},
        )
        assert len({b["ward"] for b in opened.json()}) > 1

    def test_an_override_lifts_the_implicit_department_filter_too(self, flow):
        client, repo = flow
        for dept in ("ED", "ICU"):
            repo.queue_store.append(
                {
                    "patient_id": f"MRN-{dept}",
                    "acuity": 2,
                    "dept": dept,
                    "status": "waiting",
                    "created_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                    "created_by": "seed",
                }
            )

        scoped = client.get("/queue", headers=auth("clinician", departments=["ED"]))
        assert {i["dept"] for i in scoped.json()["items"]} == {"ED"}

        opened = client.get(
            "/queue",
            headers={
                **auth("clinician", departments=["ED"]),
                "X-Break-Glass-Reason": REASON,
            },
        )
        assert {i["dept"] for i in opened.json()["items"]} == {"ED", "ICU"}

    def test_the_list_path_override_is_also_audited(self, flow, caplog):
        """The filtered-list path must not be a quiet way to widen access."""
        client, _ = flow
        with caplog.at_level(logging.WARNING, logger="medicore.audit"):
            client.get(
                "/beds",
                headers={**auth("clinician", wards=["A"]), "X-Break-Glass-Reason": REASON},
            )
        assert any("break_glass_access" in r.message for r in caplog.records)


class TestItIsNotABackdoor:
    def test_a_blank_reason_does_not_override(self, flow):
        client, _ = flow
        r = client.get(
            "/beds",
            params={"ward": "ICU"},
            headers={**auth("clinician", wards=["A"]), "X-Break-Glass-Reason": "   "},
        )
        assert r.status_code == 403

    def test_a_token_reason_is_rejected_outright(self, flow):
        """Rejected, not ignored: silently downgrading "x" to a normal 403
        would leave the clinician with no idea the override was discarded."""
        client, _ = flow
        r = client.get(
            "/beds",
            params={"ward": "ICU"},
            headers={**auth("clinician", wards=["A"]), "X-Break-Glass-Reason": "x"},
        )
        assert r.status_code == 400
        assert "reason" in r.json()["detail"].lower()

    def test_it_cannot_escalate_a_role(self, flow):
        """Break-glass relaxes data scope, never authorisation level."""
        client, _ = flow
        r = client.get(
            "/beds",
            headers={**auth("viewer"), "X-Break-Glass-Reason": REASON},
        )
        assert r.status_code == 403

    def test_it_does_not_bypass_authentication(self, flow):
        client, _ = flow
        r = client.get("/beds", headers={"X-Break-Glass-Reason": REASON})
        assert r.status_code == 401

    def test_it_is_per_request_not_a_standing_privilege(self, flow):
        client, _ = flow
        headers = auth("clinician", wards=["A"])
        assert (
            client.get(
                "/beds",
                params={"ward": "ICU"},
                headers={**headers, "X-Break-Glass-Reason": REASON},
            ).status_code
            == 200
        )
        # The very next request, same token, no header: back to enforced.
        assert (
            client.get("/beds", params={"ward": "ICU"}, headers=headers).status_code == 403
        )


class TestAuditTrail:
    def test_the_override_is_logged_with_its_reason(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.WARNING, logger="medicore.audit"):
            client.get(
                "/beds",
                params={"ward": "ICU"},
                headers={**auth("clinician", wards=["A"]), "X-Break-Glass-Reason": REASON},
            )
        assert any("break_glass_access" in r.message for r in caplog.records)
        assert any(REASON in r.message for r in caplog.records)

    def test_the_request_record_is_marked_break_glass(self, flow, caplog):
        """Distinct outcome so every override can be listed without wading
        through routine access."""
        client, _ = flow
        with caplog.at_level(logging.WARNING, logger="medicore.audit"):
            client.get(
                "/beds",
                params={"ward": "ICU"},
                headers={**auth("clinician", wards=["A"]), "X-Break-Glass-Reason": REASON},
            )

        outcomes = []
        for record in caplog.records:
            try:
                payload = json.loads(record.message)
            except (ValueError, TypeError):
                continue
            if payload.get("event") == "http_request":
                outcomes.append(payload.get("outcome"))
        assert "break_glass" in outcomes

    def test_the_scope_that_was_overridden_is_recorded(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.WARNING, logger="medicore.audit"):
            client.get(
                "/beds",
                params={"ward": "ICU"},
                headers={**auth("clinician", wards=["A"]), "X-Break-Glass-Reason": REASON},
            )
        assert any("ward:ICU" in r.message for r in caplog.records)

    def test_it_is_logged_at_warning_so_it_can_be_alerted_on(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get(
                "/beds",
                params={"ward": "ICU"},
                headers={**auth("clinician", wards=["A"]), "X-Break-Glass-Reason": REASON},
            )
        levels = [r.levelno for r in caplog.records if "break_glass" in r.message]
        assert levels and min(levels) >= logging.WARNING

    def test_an_ordinary_request_is_not_marked_break_glass(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get("/beds", headers=auth("clinician"))
        assert not any("break_glass" in r.message for r in caplog.records)


class TestReasonValidation:
    def test_reason_helper_accepts_a_specific_reason(self):
        from backend.common.deps import break_glass_reason

        class _Req:
            headers = {"x-break-glass-reason": REASON}

        assert break_glass_reason(_Req()) == REASON

    def test_reason_helper_returns_none_when_absent(self):
        from backend.common.deps import break_glass_reason

        class _Req:
            headers: dict[str, str] = {}

        assert break_glass_reason(_Req()) is None

    def test_an_overlong_reason_is_truncated_rather_than_rejected(self):
        """The clinician's access should not fail over a verbose note."""
        from backend.common.deps import break_glass_reason

        class _Req:
            headers = {"x-break-glass-reason": "y" * 5000}

        assert len(break_glass_reason(_Req())) == 500
