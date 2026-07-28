"""Patient flow: persistence, concurrency semantics and operational endpoints."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.common.security import create_access_token
from backend.tests.fakes import FakePatientFlowRepository


def auth(*roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token('nurse.1', roles=list(roles) or ['clinician'])}"}


@pytest.fixture()
def app_and_repo():
    import backend.services.patient_flow.main as pf

    repo = FakePatientFlowRepository(
        beds=[
            {"bed_id": "A-001", "ward": "A"},
            {"bed_id": "A-002", "ward": "A"},
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


class TestBedLayout:
    def test_ids_are_deterministic_across_replicas(self):
        """Every replica must derive identical bed ids, or each pod shows a
        different ward. This was previously random per process."""
        from backend.services.patient_flow.main import build_bed_documents

        assert build_bed_documents() == build_bed_documents()
        ids = [d["bed_id"] for d in build_bed_documents()]
        assert len(ids) == len(set(ids))
        assert "A-001" in ids

    def test_layout_parsing(self, monkeypatch):
        from backend.common.config import Settings

        s = Settings(bed_layout="A:2, ICU:1")
        assert s.parsed_bed_layout == [("A", 2), ("ICU", 1)]

    def test_invalid_layout_is_rejected_loudly(self):
        from backend.common.config import Settings

        with pytest.raises(ValueError, match="expected 'WARD:COUNT'"):
            _ = Settings(bed_layout="A:notanumber").parsed_bed_layout

    def test_negative_count_is_rejected(self):
        from backend.common.config import Settings

        with pytest.raises(ValueError, match="cannot be negative"):
            _ = Settings(bed_layout="A:-3").parsed_bed_layout


class TestSeeding:
    async def _seed_twice(self, repo):
        docs = [{"bed_id": "A-001", "ward": "A"}]
        first = await repo.seed_beds(docs)
        second = await repo.seed_beds(docs)
        return first, second

    def test_seeding_is_idempotent(self, app_and_repo):
        """Restarting a pod must not duplicate beds or reset occupancy."""
        import asyncio

        repo = FakePatientFlowRepository()
        first, second = asyncio.run(self._seed_twice(repo))
        assert (first, second) == (1, 0)

    def test_seeding_preserves_live_occupancy(self, app_and_repo):
        import asyncio

        client, repo = app_and_repo
        client.patch(
            "/beds/A-001",
            json={"occupied": True, "patient_id": "MRN-1"},
            headers=auth("clinician"),
        )
        asyncio.run(repo.seed_beds([{"bed_id": "A-001", "ward": "A"}]))
        bed = client.get("/beds/A-001", headers=auth("clinician")).json()
        assert bed["occupied"] is True
        assert bed["patient_id"] == "MRN-1"


class TestBeds:
    def test_startup_seeds_the_configured_ward_layout(self, app_and_repo):
        """Startup must create every bed in BED_LAYOUT, not just fixture beds."""
        from backend.services.patient_flow.main import build_bed_documents

        client, _ = app_and_repo
        beds = client.get("/beds", headers=auth()).json()
        assert len(beds) == len(build_bed_documents())

    def test_filter_by_ward(self, app_and_repo):
        client, _ = app_and_repo
        icu = client.get("/beds", params={"ward": "ICU"}, headers=auth()).json()
        assert icu, "expected at least one ICU bed from the default layout"
        assert all(b["ward"] == "ICU" for b in icu)
        assert "ICU-001" in [b["bed_id"] for b in icu]

    def test_filter_by_occupancy(self, app_and_repo):
        client, _ = app_and_repo
        client.patch(
            "/beds/A-001",
            json={"occupied": True, "patient_id": "MRN-1"},
            headers=auth(),
        )
        free = client.get("/beds", params={"occupied": False}, headers=auth()).json()
        assert "A-001" not in [b["bed_id"] for b in free]

    def test_assigning_requires_a_patient_id(self, app_and_repo):
        client, _ = app_and_repo
        r = client.patch("/beds/A-001", json={"occupied": True}, headers=auth())
        assert r.status_code == 422
        assert "patient_id is required" in r.json()["detail"]

    def test_releasing_clears_the_patient(self, app_and_repo):
        client, _ = app_and_repo
        client.patch(
            "/beds/A-001", json={"occupied": True, "patient_id": "MRN-1"}, headers=auth()
        )
        r = client.patch("/beds/A-001", json={"occupied": False}, headers=auth())
        assert r.json()["patient_id"] is None

    def test_unknown_bed_is_404(self, app_and_repo):
        client, _ = app_and_repo
        assert client.get("/beds/NOPE", headers=auth()).status_code == 404

    def test_state_persists_across_requests(self, app_and_repo):
        """The core fix: occupancy lives in the database, not process memory."""
        client, _ = app_and_repo
        client.patch(
            "/beds/A-001", json={"occupied": True, "patient_id": "MRN-7"}, headers=auth()
        )
        assert client.get("/beds/A-001", headers=auth()).json()["occupied"] is True


class TestBedConcurrency:
    def test_conditional_update_rejects_a_lost_update(self, app_and_repo):
        """Two clinicians assigning the same bed must not both succeed."""
        client, _ = app_and_repo
        first = client.patch(
            "/beds/A-001",
            json={"occupied": True, "patient_id": "MRN-1", "expected_occupied": False},
            headers=auth(),
        )
        assert first.status_code == 200

        second = client.patch(
            "/beds/A-001",
            json={"occupied": True, "patient_id": "MRN-2", "expected_occupied": False},
            headers=auth(),
        )
        assert second.status_code == 409
        assert "another user" in second.json()["detail"]

        # The first assignment stands.
        assert client.get("/beds/A-001", headers=auth()).json()["patient_id"] == "MRN-1"

    def test_unconditional_update_still_works(self, app_and_repo):
        client, _ = app_and_repo
        r = client.patch(
            "/beds/A-001", json={"occupied": True, "patient_id": "MRN-1"}, headers=auth()
        )
        assert r.status_code == 200


class TestQueue:
    def _add(self, client, pid, acuity, dept="ED", reason=None):
        """Enqueue a patient.

        Supplies a reason automatically for urgent acuities: the API requires
        one at acuity <= 2, so tests about ordering or conflicts should not
        have to restate it.
        """
        body = {"patient_id": pid, "acuity": acuity, "dept": dept}
        if reason is not None:
            body["reason"] = reason
        elif acuity <= 2:
            body["reason"] = "Test escalation with a substantive reason"
        return client.post("/queue", json=body, headers=auth())

    @staticmethod
    def _complete(client, pid, disposition="discharged", note=None, **kw):
        """Complete a queue entry. Disposition is required by the API."""
        body = {"disposition": disposition}
        if note is not None:
            body["disposition_note"] = note
        return client.post(f"/queue/{pid}/complete", json=body, headers=auth(), **kw)

    def test_enqueue_and_list(self, app_and_repo):
        client, _ = app_and_repo
        assert self._add(client, "MRN-1", 3).status_code == 201
        body = client.get("/queue", headers=auth()).json()
        assert body["count"] == 1
        assert body["total"] == 1

    def test_ordered_by_acuity_then_arrival(self, app_and_repo):
        client, _ = app_and_repo
        self._add(client, "MRN-low", 5)
        self._add(client, "MRN-urgent", 1)
        self._add(client, "MRN-mid", 3)
        order = [i["patient_id"] for i in client.get("/queue", headers=auth()).json()["items"]]
        assert order == ["MRN-urgent", "MRN-mid", "MRN-low"]

    def test_duplicate_waiting_patient_is_rejected(self, app_and_repo):
        """A patient must not occupy two queue slots."""
        client, _ = app_and_repo
        self._add(client, "MRN-1", 3)
        second = self._add(client, "MRN-1", 2)
        assert second.status_code == 409

    def test_requeue_allowed_after_completion(self, app_and_repo):
        client, _ = app_and_repo
        self._add(client, "MRN-1", 3)
        self._complete(client, "MRN-1")
        assert self._add(client, "MRN-1", 2).status_code == 201

    def test_total_reflects_full_queue_not_the_page(self, app_and_repo):
        client, _ = app_and_repo
        for i in range(5):
            self._add(client, f"MRN-{i}", 3)
        body = client.get("/queue", params={"limit": 2}, headers=auth()).json()
        assert body["count"] == 2
        assert body["total"] == 5

    def test_department_filter(self, app_and_repo):
        client, _ = app_and_repo
        self._add(client, "MRN-ed", 3, dept="ED")
        self._add(client, "MRN-icu", 3, dept="ICU")
        body = client.get("/queue", params={"dept": "ICU"}, headers=auth()).json()
        assert [i["patient_id"] for i in body["items"]] == ["MRN-icu"]

    def test_records_who_added_the_patient(self, app_and_repo):
        client, repo = app_and_repo
        self._add(client, "MRN-1", 3)
        assert repo.queue_store[0]["created_by"] == "nurse.1"

    @pytest.mark.parametrize(
        "payload",
        [
            {"patient_id": "", "acuity": 3, "dept": "ED"},
            {"patient_id": "MRN-1", "acuity": 0, "dept": "ED"},
            {"patient_id": "MRN-1", "acuity": 6, "dept": "ED"},
            {"patient_id": "MRN-1", "acuity": 3, "dept": ""},
            {"patient_id": "has spaces", "acuity": 3, "dept": "ED"},
            {"acuity": 3, "dept": "ED"},
        ],
    )
    def test_invalid_payloads_rejected(self, app_and_repo, payload):
        client, _ = app_and_repo
        assert client.post("/queue", json=payload, headers=auth()).status_code == 422

    def test_claim_takes_the_most_urgent(self, app_and_repo):
        client, _ = app_and_repo
        self._add(client, "MRN-low", 5)
        self._add(client, "MRN-urgent", 1)
        claimed = client.post("/queue/claim", params={"dept": "ED"}, headers=auth())
        assert claimed.json()["item"]["patient_id"] == "MRN-urgent"

    def test_claim_is_exclusive(self, app_and_repo):
        """Two clinicians must never be handed the same patient."""
        client, _ = app_and_repo
        self._add(client, "MRN-1", 1)
        first = client.post("/queue/claim", params={"dept": "ED"}, headers=auth())
        second = client.post("/queue/claim", params={"dept": "ED"}, headers=auth())
        assert first.status_code == 200
        assert second.status_code == 404

    def test_claim_on_empty_queue(self, app_and_repo):
        client, _ = app_and_repo
        assert client.post("/queue/claim", params={"dept": "ED"}, headers=auth()).status_code == 404

    def test_complete_unknown_patient(self, app_and_repo):
        client, _ = app_and_repo
        assert self._complete(client, "MRN-nope").status_code == 404

    def test_claimed_patient_leaves_the_waiting_list(self, app_and_repo):
        client, _ = app_and_repo
        self._add(client, "MRN-1", 1)
        client.post("/queue/claim", params={"dept": "ED"}, headers=auth())
        assert client.get("/queue", headers=auth()).json()["total"] == 0


class TestOperationalEndpoints:
    def test_health_is_liveness_only(self, app_and_repo):
        """Liveness must not depend on the database, or an outage would
        restart every healthy pod."""
        client, repo = app_and_repo
        repo.ping_error = RuntimeError("mongo down")
        assert client.get("/health").status_code == 200

    def test_ready_reports_database_failure(self, app_and_repo):
        client, repo = app_and_repo
        repo.ping_error = RuntimeError("mongo down")
        r = client.get("/ready")
        assert r.status_code == 503
        assert r.json()["database"] == "unavailable"

    def test_ready_is_ok_when_healthy(self, app_and_repo):
        client, _ = app_and_repo
        r = client.get("/ready")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_database_errors_return_503_not_500(self, app_and_repo):
        from pymongo.errors import PyMongoError

        client, repo = app_and_repo
        repo.fail_with = PyMongoError("connection lost")
        r = client.get("/beds", headers=auth())
        assert r.status_code == 503

    def test_database_errors_do_not_leak_internals(self, app_and_repo):
        """Driver messages can contain hostnames and credentials."""
        from pymongo.errors import PyMongoError

        client, repo = app_and_repo
        repo.fail_with = PyMongoError("mongodb://admin:hunter2@10.0.0.5 refused")
        body = client.get("/beds", headers=auth()).text
        assert "hunter2" not in body
        assert "10.0.0.5" not in body
