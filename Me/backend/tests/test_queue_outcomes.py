"""Closing the clinical loop: escalation evidence and completion outcomes.

Before this, the queue recorded actions but not reasoning or results. An
escalation discarded the NEWS2 score that justified it, and completion set
only ``status: completed`` — so "admitted to ICU" and "walked out unseen"
were the same record.

These tests are mostly about what the API now *refuses* to accept, because a
disposition field that takes any string is a reporting liability rather than a
clinical record.

See docs/design/closing-the-clinical-loop.md.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.common.security import create_access_token
from backend.services.patient_flow.main import DISPOSITIONS
from backend.tests.fakes import FakePatientFlowRepository

REASON = "Rising NEWS2 with new confusion, needs urgent review"


@pytest.fixture()
def flow():
    import backend.services.patient_flow.main as pf

    repo = FakePatientFlowRepository(beds=[{"bed_id": "A-001", "ward": "A"}])
    pf.app.dependency_overrides[pf.get_repository] = lambda: repo
    pf._repository = repo
    try:
        with TestClient(pf.app, raise_server_exceptions=False) as client:
            yield client, repo
    finally:
        pf.app.dependency_overrides.clear()
        pf._repository = None


def auth(sub: str = "dr.smith", roles=("clinician",)) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(sub, roles=list(roles))}"}


def enqueue(client, pid="MRN-1", acuity=3, dept="ED", **extra) -> Any:
    body: dict[str, Any] = {"patient_id": pid, "acuity": acuity, "dept": dept}
    if acuity <= 2 and "reason" not in extra:
        body["reason"] = REASON
    body.update(extra)
    return client.post("/queue", json=body, headers=auth())


def complete(client, pid="MRN-1", disposition="discharged", sub="dr.smith", **extra):
    body: dict[str, Any] = {"disposition": disposition}
    body.update(extra)
    return client.post(f"/queue/{pid}/complete", json=body, headers=auth(sub))


def records(caplog) -> list[dict]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "medicore.audit" and r.getMessage().startswith("{")
    ]


# ---------------------------------------------------------------------------
# Escalation evidence
# ---------------------------------------------------------------------------


class TestEscalationCarriesItsEvidence:
    def test_the_news2_score_that_justified_it_is_stored(self, flow):
        """The gap this closes: a charge nurse seeing an ESI-1 could not tell
        why it was an ESI-1."""
        client, repo = flow
        r = enqueue(
            client,
            acuity=1,
            reason=REASON,
            news2_score=9,
            news2_band="high",
            red_flag=True,
            vitals_snapshot={"respiratory_rate": 28, "spo2": 89, "pulse": 132},
        )
        assert r.status_code == 201
        stored = repo.queue_store[0]
        assert stored["news2_score"] == 9
        assert stored["news2_band"] == "high"
        assert stored["red_flag"] is True
        assert stored["vitals_snapshot"]["spo2"] == 89
        assert stored["reason"] == REASON

    def test_evidence_is_optional_so_clinical_judgement_still_works(self, flow):
        """Blocking an escalation with no NEWS2 would be unsafe — clinicians
        escalate on judgement, and they would route around the block."""
        client, repo = flow
        assert enqueue(client, acuity=1, reason=REASON).status_code == 201
        stored = repo.queue_store[0]
        assert "news2_score" not in stored
        assert "vitals_snapshot" not in stored

    def test_absent_evidence_is_omitted_not_nulled(self, flow):
        """`null` would make "no NEWS2 taken" indistinguishable from "taken
        and lost"."""
        client, repo = flow
        enqueue(client, acuity=4)
        stored = repo.queue_store[0]
        for field in ("news2_score", "news2_band", "red_flag", "vitals_snapshot"):
            assert field not in stored

    def test_a_reason_is_required_for_urgent_escalations(self, flow):
        client, _ = flow
        r = client.post(
            "/queue",
            json={"patient_id": "MRN-1", "acuity": 1, "dept": "ED"},
            headers=auth(),
        )
        assert r.status_code == 422
        assert "reason is required" in r.text

    @pytest.mark.parametrize("acuity", [3, 4, 5])
    def test_routine_escalations_do_not_need_a_reason(self, flow, acuity):
        """Requiring one everywhere trains people to type '.' to get past the
        field, which degrades the whole dataset."""
        client, _ = flow
        r = client.post(
            "/queue",
            json={"patient_id": f"MRN-{acuity}", "acuity": acuity, "dept": "ED"},
            headers=auth(),
        )
        assert r.status_code == 201

    def test_a_token_reason_is_rejected(self, flow):
        client, _ = flow
        r = enqueue(client, acuity=1, reason="x")
        assert r.status_code == 422
        assert "at least" in r.text

    def test_a_whitespace_reason_does_not_satisfy_the_requirement(self, flow):
        client, _ = flow
        assert enqueue(client, acuity=2, reason="          ").status_code == 422

    def test_an_unknown_news2_band_is_rejected(self, flow):
        client, _ = flow
        assert enqueue(client, acuity=3, news2_band="catastrophic").status_code == 422

    def test_impossible_vitals_cannot_justify_an_escalation(self, flow):
        """Bounds mirror the CDS service: a value that could not be scored
        must not be recorded as the evidence for a decision."""
        client, _ = flow
        r = enqueue(client, acuity=3, vitals_snapshot={"spo2": 500})
        assert r.status_code == 422

    def test_unknown_vitals_fields_are_rejected(self, flow):
        client, _ = flow
        r = enqueue(client, acuity=3, vitals_snapshot={"blood_type": "O-"})
        assert r.status_code == 422

    def test_the_escalating_clinician_is_recorded_from_the_token(self, flow):
        client, repo = flow
        client.post(
            "/queue",
            json={"patient_id": "MRN-1", "acuity": 3, "dept": "ED", "created_by": "dr.someone.else"},
            headers=auth("dr.real"),
        )
        assert repo.queue_store[0]["created_by"] == "dr.real"


# ---------------------------------------------------------------------------
# Completion outcomes
# ---------------------------------------------------------------------------


class TestCompletionRecordsWhatHappened:
    def test_a_disposition_is_required(self, flow):
        """The whole point: a patient must not leave the queue with no record
        of the outcome."""
        client, _ = flow
        enqueue(client)
        r = client.post("/queue/MRN-1/complete", json={}, headers=auth())
        assert r.status_code == 422

    @pytest.mark.parametrize("disposition", sorted(DISPOSITIONS - {"other", "left_without_being_seen"}))
    def test_each_valid_disposition_is_accepted(self, flow, disposition):
        client, _ = flow
        enqueue(client)
        r = complete(client, disposition=disposition)
        assert r.status_code == 200
        assert r.json()["item"]["disposition"] == disposition

    def test_an_unknown_disposition_is_rejected(self, flow):
        """A free-text field cannot be reported on, so the set is closed."""
        client, _ = flow
        enqueue(client)
        r = complete(client, disposition="wandered off somewhere")
        assert r.status_code == 422
        assert "must be one of" in r.text

    def test_left_without_being_seen_requires_a_note(self, flow):
        """The outcome a department is most accountable for needs context."""
        client, _ = flow
        enqueue(client)
        assert complete(client, disposition="left_without_being_seen").status_code == 422

    def test_other_requires_a_note(self, flow):
        client, _ = flow
        enqueue(client)
        assert complete(client, disposition="other").status_code == 422

    def test_a_note_satisfies_the_requirement(self, flow):
        client, _ = flow
        enqueue(client)
        r = complete(
            client,
            disposition="left_without_being_seen",
            disposition_note="Called three times, not present in waiting area",
        )
        assert r.status_code == 200
        assert "Called three times" in r.json()["item"]["disposition_note"]

    def test_the_completing_clinician_comes_from_the_token(self, flow):
        """A record that could claim to be someone else's decision is worse
        than no record."""
        client, _ = flow
        enqueue(client)
        r = client.post(
            "/queue/MRN-1/complete",
            json={"disposition": "admitted", "completed_by": "dr.someone.else"},
            headers=auth("dr.real"),
        )
        assert r.json()["item"]["completed_by"] == "dr.real"

    def test_time_to_completion_is_derived_server_side(self, flow):
        client, _ = flow
        enqueue(client)
        body = complete(client, disposition="admitted").json()
        assert body["item"]["time_to_completion_seconds"] >= 0

    def test_a_client_cannot_massage_the_duration(self, flow):
        client, _ = flow
        enqueue(client)
        body = client.post(
            "/queue/MRN-1/complete",
            json={"disposition": "admitted", "time_to_completion_seconds": -999},
            headers=auth(),
        ).json()
        assert body["item"]["time_to_completion_seconds"] >= 0

    def test_completing_twice_is_a_conflict(self, flow):
        client, _ = flow
        enqueue(client)
        assert complete(client, disposition="admitted").status_code == 200
        assert complete(client, disposition="discharged").status_code == 409

    def test_a_second_attempt_does_not_overwrite_the_outcome(self, flow):
        """A recorded outcome must not be quietly changed after the fact."""
        client, repo = flow
        enqueue(client)
        complete(client, disposition="admitted", sub="dr.first")
        complete(client, disposition="deceased", sub="dr.second")
        stored = repo.queue_store[0]
        assert stored["disposition"] == "admitted"
        assert stored["completed_by"] == "dr.first"

    def test_completing_an_unknown_patient_is_still_404(self, flow):
        client, _ = flow
        assert complete(client, pid="MRN-ghost").status_code == 404


# ---------------------------------------------------------------------------
# History and reporting
# ---------------------------------------------------------------------------


class TestHistoryAndStats:
    def test_history_returns_the_full_lifecycle(self, flow):
        client, _ = flow
        enqueue(client, acuity=2, news2_score=7)
        complete(client, disposition="admitted")
        body = client.get("/queue/MRN-1/history", headers=auth()).json()
        assert body["count"] == 1
        entry = body["entries"][0]
        assert entry["news2_score"] == 7
        assert entry["disposition"] == "admitted"

    def test_history_covers_repeat_attendances_newest_first(self, flow):
        client, _ = flow
        enqueue(client, acuity=4)
        complete(client, disposition="discharged")
        enqueue(client, acuity=2, reason=REASON)
        complete(client, disposition="admitted")
        entries = client.get("/queue/MRN-1/history", headers=auth()).json()["entries"]
        assert [e["disposition"] for e in entries] == ["admitted", "discharged"]

    def test_history_of_an_unknown_patient_is_empty_not_an_error(self, flow):
        client, _ = flow
        r = client.get("/queue/MRN-none/history", headers=auth())
        assert r.status_code == 200
        assert r.json()["entries"] == []

    def test_stats_count_by_disposition(self, flow):
        client, _ = flow
        for i, disposition in enumerate(["admitted", "admitted", "discharged"]):
            enqueue(client, pid=f"MRN-{i}")
            complete(client, pid=f"MRN-{i}", disposition=disposition)
        stats = client.get("/queue/stats", headers=auth()).json()
        assert stats["completed"] == 3
        assert stats["by_disposition"]["admitted"] == 2
        assert stats["by_disposition"]["discharged"] == 1

    def test_stats_report_the_lwbs_rate(self, flow):
        """The headline safety number for an emergency department."""
        client, _ = flow
        enqueue(client, pid="MRN-a")
        complete(client, pid="MRN-a", disposition="admitted")
        enqueue(client, pid="MRN-b")
        complete(
            client,
            pid="MRN-b",
            disposition="left_without_being_seen",
            disposition_note="Not present when called",
        )
        stats = client.get("/queue/stats", headers=auth()).json()
        assert stats["left_without_being_seen_rate"] == 0.5

    def test_an_empty_window_returns_zeros_not_an_error(self, flow):
        client, _ = flow
        stats = client.get("/queue/stats", headers=auth()).json()
        assert stats["completed"] == 0
        assert stats["left_without_being_seen_rate"] == 0.0
        # None, not 0: "no completions" and "instant completions" differ.
        assert stats["median_seconds"] is None

    def test_stats_count_waiting_patients(self, flow):
        client, _ = flow
        enqueue(client, pid="MRN-waiting")
        assert client.get("/queue/stats", headers=auth()).json()["waiting"] == 1

    def test_stats_can_be_scoped_to_a_department(self, flow):
        client, _ = flow
        enqueue(client, pid="MRN-ed", dept="ED")
        complete(client, pid="MRN-ed", disposition="admitted")
        enqueue(client, pid="MRN-icu", dept="ICU")
        complete(client, pid="MRN-icu", disposition="admitted")
        stats = client.get("/queue/stats", params={"dept": "ED"}, headers=auth()).json()
        assert stats["completed"] == 1

    def test_stats_route_is_not_shadowed_by_a_path_parameter(self):
        """Guards the declaration order: a future GET /queue/{patient_id}
        placed above /queue/stats would silently treat "stats" as an id."""
        import backend.services.patient_flow.main as pf

        paths = [
            getattr(r, "path", "")
            for r in pf.app.routes
            if getattr(r, "path", "").startswith("/queue")
            and "GET" in (getattr(r, "methods", set()) or set())
        ]
        assert "/queue/stats" in paths
        assert paths.index("/queue/stats") < min(
            (i for i, p in enumerate(paths) if "{patient_id}" in p),
            default=len(paths),
        )


# ---------------------------------------------------------------------------
# Access control and audit
# ---------------------------------------------------------------------------


class TestAccessAndAudit:
    @pytest.mark.parametrize(
        "method,path",
        [("get", "/queue/MRN-1/history"), ("get", "/queue/stats")],
    )
    def test_authentication_is_required(self, flow, method, path):
        client, _ = flow
        assert getattr(client, method)(path).status_code == 401

    def test_a_viewer_cannot_read_the_history(self, flow):
        client, _ = flow
        r = client.get("/queue/MRN-1/history", headers=auth(roles=["viewer"]))
        assert r.status_code == 403

    def test_a_viewer_cannot_complete_a_patient(self, flow):
        client, _ = flow
        enqueue(client)
        r = client.post(
            "/queue/MRN-1/complete",
            json={"disposition": "admitted"},
            headers=auth(roles=["viewer"]),
        )
        assert r.status_code == 403

    def test_the_clinical_reason_is_never_logged(self, flow, caplog):
        """Free text may contain PHI; only the fact of the write is auditable."""
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            enqueue(client, acuity=1, reason="Patient SECRET-DETAIL collapsed in triage")
        assert "SECRET-DETAIL" not in "\n".join(r.getMessage() for r in caplog.records)

    def test_the_disposition_note_is_never_logged(self, flow, caplog):
        client, _ = flow
        enqueue(client)
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            complete(
                client,
                disposition="other",
                disposition_note="Transferred to SECRET-FACILITY",
            )
        assert "SECRET-FACILITY" not in "\n".join(r.getMessage() for r in caplog.records)

    def test_completion_is_audited_against_the_patient(self, flow, caplog):
        from backend.common.middleware import audit_reference

        client, _ = flow
        enqueue(client)
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            complete(client, disposition="admitted")
        rows = [r for r in records(caplog) if r["path"] == "/queue/{id}"]
        assert rows
        assert rows[-1]["patient_ref"] == audit_reference("MRN-1")
