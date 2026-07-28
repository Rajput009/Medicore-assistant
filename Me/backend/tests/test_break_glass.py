"""Break-glass: emergency override of ward/department scope.

A control like this is only as good as its limits, so most of these tests are
about what break-glass must *refuse* to do:

  * it must not grant a role the caller does not have
  * it must not work without a substantive reason
  * it must not turn a list endpoint into cross-ward browsing
  * it must not be silent

See backend/common/breakglass.py for the reasoning behind each.
"""

from __future__ import annotations

import json
import logging

import pytest
from starlette.testclient import TestClient

from backend.common.breakglass import (
    BREAK_GLASS_HEADER,
    MIN_REASON_LENGTH,
    BreakGlassError,
    parse_declaration,
)
from backend.common.security import create_access_token
from backend.tests.fakes import FakePatientFlowRepository

REASON = "Cardiac arrest in bay 4, covering clinician unavailable"


def _request(headers: dict[str, str]):
    """Minimal Starlette request carrying the given headers."""
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


# ---------------------------------------------------------------------------
# Parsing the declaration
# ---------------------------------------------------------------------------


class TestParseDeclaration:
    def test_absent_header_is_not_a_declaration(self):
        assert parse_declaration(_request({})) is None

    def test_valid_reason_is_accepted(self):
        declaration = parse_declaration(_request({BREAK_GLASS_HEADER: REASON}))
        assert declaration is not None
        assert declaration.reason == REASON

    def test_a_trivial_reason_is_rejected(self):
        """'x' in an audit column looks like compliance while providing
        nothing; refuse it rather than record it."""
        with pytest.raises(BreakGlassError, match="at least"):
            parse_declaration(_request({BREAK_GLASS_HEADER: "urgent"}))

    def test_whitespace_only_reason_is_rejected(self):
        with pytest.raises(BreakGlassError, match="requires a reason"):
            parse_declaration(_request({BREAK_GLASS_HEADER: "        "}))

    def test_overlong_reason_is_rejected(self):
        with pytest.raises(BreakGlassError, match="at most"):
            parse_declaration(_request({BREAK_GLASS_HEADER: "y" * 5000}))

    def test_newlines_are_collapsed_to_prevent_log_forging(self):
        """The reason is written to logs; a newline would let the caller
        fabricate additional log lines."""
        hostile = "Real reason here\n{\"event\": \"forged\", \"sub\": \"admin\"}"
        declaration = parse_declaration(_request({BREAK_GLASS_HEADER: hostile}))
        assert "\n" not in declaration.reason
        assert declaration.reason.startswith("Real reason here")

    def test_disabled_deployment_rejects_rather_than_ignores(self):
        """Silently ignoring the header would leave the caller believing they
        had emergency access when they did not."""
        with pytest.raises(BreakGlassError, match="disabled"):
            parse_declaration(_request({BREAK_GLASS_HEADER: REASON}), enabled=False)

    def test_disabled_deployment_still_ignores_an_absent_header(self):
        assert parse_declaration(_request({}), enabled=False) is None

    def test_reason_at_the_exact_minimum_is_accepted(self):
        reason = "a" * MIN_REASON_LENGTH
        assert parse_declaration(_request({BREAK_GLASS_HEADER: reason})).reason == reason


# ---------------------------------------------------------------------------
# Enforcement through patient-flow
# ---------------------------------------------------------------------------


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


def headers(*, wards=None, departments=None, roles=("clinician",), reason=None):
    token = create_access_token(
        "dr.scoped",
        roles=list(roles),
        wards=list(wards) if wards else None,
        departments=list(departments) if departments else None,
    )
    h = {"Authorization": f"Bearer {token}"}
    if reason is not None:
        h[BREAK_GLASS_HEADER] = reason
    return h


def records(caplog) -> list[dict]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "medicore.audit" and r.getMessage().startswith("{")
    ]


class TestScopeOverride:
    def test_out_of_ward_bed_update_is_denied_without_break_glass(self, flow):
        client, _ = flow
        r = client.patch(
            "/beds/ICU-001",
            json={"occupied": True, "patient_id": "MRN-1"},
            headers=headers(wards=["A"]),
        )
        assert r.status_code == 403

    def test_break_glass_grants_the_out_of_ward_update(self, flow):
        client, repo = flow
        r = client.patch(
            "/beds/ICU-001",
            json={"occupied": True, "patient_id": "MRN-1"},
            headers=headers(wards=["A"], reason=REASON),
        )
        assert r.status_code == 200
        assert repo.beds_store["ICU-001"]["occupied"] is True

    def test_break_glass_grants_out_of_department_enqueue(self, flow):
        client, _ = flow
        r = client.post(
            "/queue",
            json={"patient_id": "MRN-2", "acuity": 1, "dept": "ICU", "reason": "Deteriorating observations requiring urgent review"},
            headers=headers(departments=["ED"], reason=REASON),
        )
        assert r.status_code in (200, 201)

    def test_break_glass_grants_out_of_department_claim(self, flow):
        client, _ = flow
        r = client.post(
            "/queue/claim",
            params={"dept": "ICU"},
            headers=headers(departments=["ED"], reason=REASON),
        )
        # 200 (claimed) or 204/404 (nothing waiting) — the point is not 403.
        assert r.status_code != 403

    def test_in_scope_requests_are_unaffected(self, flow):
        client, _ = flow
        r = client.patch(
            "/beds/A-001",
            json={"occupied": True, "patient_id": "MRN-3"},
            headers=headers(wards=["A"]),
        )
        assert r.status_code == 200

    def test_a_bad_reason_is_a_400_not_a_403(self, flow):
        """A clinician who typo'd the header needs to know that, rather than
        getting a denial they cannot account for."""
        client, _ = flow
        r = client.patch(
            "/beds/ICU-001",
            json={"occupied": True, "patient_id": "MRN-1"},
            headers=headers(wards=["A"], reason="oops"),
        )
        assert r.status_code == 400
        assert "at least" in r.json()["detail"]


class TestBreakGlassCannotEscalate:
    """The boundary that makes this a safety control and not a backdoor."""

    def test_it_does_not_grant_a_role_the_caller_lacks(self, flow):
        """A viewer stays a viewer. Break-glass widens scope, never role."""
        client, _ = flow
        r = client.patch(
            "/beds/ICU-001",
            json={"occupied": True, "patient_id": "MRN-1"},
            headers=headers(wards=["A"], roles=["viewer"], reason=REASON),
        )
        assert r.status_code == 403

    def test_an_unauthenticated_caller_gains_nothing(self, flow):
        client, _ = flow
        r = client.patch(
            "/beds/ICU-001",
            json={"occupied": True, "patient_id": "MRN-1"},
            headers={BREAK_GLASS_HEADER: REASON},
        )
        assert r.status_code == 401

    def test_list_endpoints_do_not_honour_break_glass(self, flow):
        """Emergency access is for reaching one patient now. Allowing it on a
        list would turn an override into cross-ward browsing."""
        client, _ = flow
        r = client.get("/beds", params={"ward": "ICU"}, headers=headers(wards=["A"], reason=REASON))
        assert r.status_code == 403

    def test_queue_listing_does_not_honour_break_glass(self, flow):
        client, _ = flow
        r = client.get(
            "/queue", params={"dept": "ICU"}, headers=headers(departments=["ED"], reason=REASON)
        )
        assert r.status_code == 403

    def test_scoped_listing_still_filters_to_the_caller_ward(self, flow):
        """Even with the header present, an unfiltered list stays scoped."""
        client, _ = flow
        r = client.get("/beds", headers=headers(wards=["A"], reason=REASON))
        assert r.status_code == 200
        assert {b["ward"] for b in r.json()} == {"A"}

    def test_disabled_by_configuration_refuses_the_override(self, flow, monkeypatch):
        from backend.common import config

        monkeypatch.setattr(config.settings, "break_glass_enabled", False)
        client, _ = flow
        r = client.patch(
            "/beds/ICU-001",
            json={"occupied": True, "patient_id": "MRN-1"},
            headers=headers(wards=["A"], reason=REASON),
        )
        assert r.status_code == 400
        assert "disabled" in r.json()["detail"]


class TestBreakGlassIsLoud:
    """An override nobody can review is not a control."""

    def test_the_override_is_logged_at_warning(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.WARNING, logger="medicore.audit"):
            client.patch(
                "/beds/ICU-001",
                json={"occupied": True, "patient_id": "MRN-1"},
                headers=headers(wards=["A"], reason=REASON),
            )
        assert any(
            getattr(r, "event", None) == "break_glass" and r.levelno >= logging.WARNING
            for r in caplog.records
        )

    def test_the_reason_is_recorded(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.WARNING, logger="medicore.audit"):
            client.patch(
                "/beds/ICU-001",
                json={"occupied": True, "patient_id": "MRN-1"},
                headers=headers(wards=["A"], reason=REASON),
            )
        reasons = [getattr(r, "break_glass_reason", None) for r in caplog.records]
        assert REASON in reasons

    def test_the_overridden_scope_is_recorded(self, flow, caplog):
        """Which ward was reached is the reviewer's first question."""
        client, _ = flow
        with caplog.at_level(logging.WARNING, logger="medicore.audit"):
            client.patch(
                "/beds/ICU-001",
                json={"occupied": True, "patient_id": "MRN-1"},
                headers=headers(wards=["A"], reason=REASON),
            )
        entries = [r for r in caplog.records if getattr(r, "event", None) == "break_glass"]
        assert entries
        assert entries[-1].scope_value == "ICU"
        assert entries[-1].scope_type == "ward"

    def test_the_request_audit_record_is_flagged(self, flow, caplog):
        """The flag must be on the request's own audit record, so the index
        can answer 'show me every override' without correlating log lines."""
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.patch(
                "/beds/ICU-001",
                json={"occupied": True, "patient_id": "MRN-1"},
                headers=headers(wards=["A"], reason=REASON),
            )
        flagged = [r for r in records(caplog) if r.get("break_glass")]
        assert flagged
        assert flagged[-1]["break_glass_reason"] == REASON

    def test_normal_requests_are_not_flagged(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.patch(
                "/beds/A-001",
                json={"occupied": True, "patient_id": "MRN-3"},
                headers=headers(wards=["A"]),
            )
        assert all(not r.get("break_glass") for r in records(caplog))

    def test_the_flag_reaches_the_audit_sink(self, flow, monkeypatch):
        from backend.common import middleware

        seen: list[dict] = []
        monkeypatch.setattr(middleware, "_sink", seen.append)
        client, _ = flow
        client.patch(
            "/beds/ICU-001",
            json={"occupied": True, "patient_id": "MRN-1"},
            headers=headers(wards=["A"], reason=REASON),
        )
        assert any(r.get("break_glass") for r in seen)

    def test_the_override_names_the_clinician(self, flow, caplog):
        """Regression: patient-flow has no JWT middleware — it authenticates
        through the get_principal dependency, which did not publish the caller
        to request.state. Every patient-flow audit record was therefore
        actor-less, so "who did this?" was unanswerable for beds and triage.
        """
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.patch(
                "/beds/ICU-001",
                json={"occupied": True, "patient_id": "MRN-1"},
                headers=headers(wards=["A"], reason=REASON),
            )
        flagged = [r for r in records(caplog) if r.get("break_glass")]
        assert flagged
        assert flagged[-1]["sub"] == "dr.scoped"

    def test_ordinary_flow_requests_also_name_the_clinician(self, flow, caplog):
        """The same gap affected every patient-flow route, not just overrides."""
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get("/beds", headers=headers(wards=["A"]))
        assert records(caplog)[-1]["sub"] == "dr.scoped"

    def test_a_refused_declaration_is_not_recorded_as_an_override(self, flow, caplog):
        """A rejected reason granted nothing, so it must not appear in the
        override review queue."""
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.patch(
                "/beds/ICU-001",
                json={"occupied": True, "patient_id": "MRN-1"},
                headers=headers(wards=["A"], reason="short"),
            )
        assert all(not r.get("break_glass") for r in records(caplog))
