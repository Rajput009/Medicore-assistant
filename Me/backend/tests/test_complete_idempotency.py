"""Idempotent triage completion.

``POST /queue/{id}/complete`` previously ignored ``Idempotency-Key``. When the
response to a completion was lost, the client's retry hit a queue entry that
was already completed and got a 404 — which the console reports as "no active
queue entry", i.e. the patient appears to have vanished. Replaying the stored
response instead makes the retry a no-op that tells the truth.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.common.idempotency import reset_idempotency_store
from backend.common.security import create_access_token
from backend.tests.fakes import FakePatientFlowRepository

# Completion now records what happened to the patient.
_DISPOSITION = {"disposition": "discharged"}


def _headers(key: str | None = None) -> dict[str, str]:
    token = create_access_token("dr.smith", roles=["clinician"])
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture(autouse=True)
def _clean_idempotency_store():
    reset_idempotency_store()
    yield
    reset_idempotency_store()


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


def _enqueue(client: TestClient, patient_id: str = "MRN-77") -> None:
    r = client.post(
        "/queue",
        json={
            "patient_id": patient_id,
            "acuity": 2,
            "dept": "ED",
            # Required at acuity <= 2.
            "reason": "Chest pain with abnormal observations",
        },
        headers=_headers("seed-enqueue-" + patient_id),
    )
    assert r.status_code == 201, r.text


class TestCompleteIdempotency:
    def test_retry_with_the_same_key_replays_the_first_response(self, flow):
        client, _ = flow
        _enqueue(client)

        first = client.post("/queue/MRN-77/complete", json=_DISPOSITION, headers=_headers("done-1"))
        assert first.status_code == 200

        # The lost-response scenario: identical request, same key.
        second = client.post("/queue/MRN-77/complete", json=_DISPOSITION, headers=_headers("done-1"))
        assert second.status_code == 200
        assert second.json() == first.json()

    def test_retry_without_a_key_still_404s(self, flow):
        """Documents why the key matters: no key, no replay."""
        client, _ = flow
        _enqueue(client)

        assert (
            client.post(
                "/queue/MRN-77/complete", json=_DISPOSITION, headers=_headers()
            ).status_code
            == 200
        )
        # 409, not 404: the entry exists and is already closed. Reporting
        # "not found" sent the clinician looking for a vanished patient.
        assert (
            client.post(
                "/queue/MRN-77/complete", json=_DISPOSITION, headers=_headers()
            ).status_code
            == 409
        )

    def test_a_different_key_is_a_new_intent(self, flow):
        client, _ = flow
        _enqueue(client)

        assert (
            client.post("/queue/MRN-77/complete", json=_DISPOSITION, headers=_headers("done-1")).status_code
            == 200
        )
        # Genuinely separate intent, so no replay — and the entry is already
        # closed, which is a conflict rather than a missing patient.
        assert (
            client.post("/queue/MRN-77/complete", json=_DISPOSITION, headers=_headers("done-2")).status_code
            == 409
        )

    def test_keys_do_not_leak_across_patients(self, flow):
        client, _ = flow
        _enqueue(client, "MRN-77")
        _enqueue(client, "MRN-88")

        first = client.post("/queue/MRN-77/complete", json=_DISPOSITION, headers=_headers("shared"))
        assert first.status_code == 200

        # Same key, different route: must not replay MRN-77's response.
        second = client.post("/queue/MRN-88/complete", json=_DISPOSITION, headers=_headers("shared"))
        assert second.status_code == 200
        assert second.json()["item"]["patient_id"] == "MRN-88"

    def test_completion_still_requires_authentication(self, flow):
        client, _ = flow
        assert client.post("/queue/MRN-77/complete", json=_DISPOSITION).status_code == 401
