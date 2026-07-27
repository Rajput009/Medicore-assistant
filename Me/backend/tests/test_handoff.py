"""Handoff (SBAR) note persistence.

These notes lived in browser sessionStorage, so a clinician who closed the tab
lost the handoff they had just written and the incoming shift could not read it
at all — the one thing a handoff note exists for.

The properties worth defending:

  * append-only, so "what was I told at 07:00?" survives a 09:00 edit
  * the author comes from the token, never the request body
  * they are PHI, so they are scoped, authenticated and audited like any
    other patient data
"""

from __future__ import annotations

import json
import logging

import pytest
from pymongo.errors import PyMongoError
from starlette.testclient import TestClient

from backend.common.middleware import audit_reference
from backend.common.security import create_access_token
from backend.tests.fakes import FakePatientFlowRepository

SBAR = (
    "S - Situation: 62M, chest pain since 06:00\n"
    "B - Background: known IHD\n"
    "A - Assessment: troponin pending\n"
    "R - Recommendation: repeat ECG at 10:00"
)


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


def auth(sub="dr.night", roles=("clinician",)):
    token = create_access_token(sub, roles=list(roles))
    return {"Authorization": f"Bearer {token}"}


def records(caplog) -> list[dict]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "medicore.audit" and r.getMessage().startswith("{")
    ]


# ---------------------------------------------------------------------------
# The gap this closes
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_a_note_survives_to_be_read_back(self, flow):
        """The whole point: the incoming shift can read what was written."""
        client, _ = flow
        assert client.post(
            "/handoff/MRN-1", json={"text": SBAR}, headers=auth()
        ).status_code == 201

        body = client.get("/handoff/MRN-1", headers=auth("dr.day")).json()
        assert body["note"]["text"] == SBAR
        assert body["note"]["author"] == "dr.night"

    def test_absent_note_reads_as_null_not_an_error(self, flow):
        """No handoff yet is a normal state, not a failure."""
        client, _ = flow
        r = client.get("/handoff/MRN-NONE", headers=auth())
        assert r.status_code == 200
        assert r.json()["note"] is None

    def test_notes_are_isolated_per_patient(self, flow):
        client, _ = flow
        client.post("/handoff/MRN-1", json={"text": "note one here"}, headers=auth())
        client.post("/handoff/MRN-2", json={"text": "note two here"}, headers=auth())
        assert client.get("/handoff/MRN-1", headers=auth()).json()["note"]["text"] == (
            "note one here"
        )
        assert client.get("/handoff/MRN-2", headers=auth()).json()["note"]["text"] == (
            "note two here"
        )

    def test_an_encounter_can_be_attached(self, flow):
        client, _ = flow
        client.post(
            "/handoff/MRN-1",
            json={"text": SBAR, "encounter_id": "enc-9"},
            headers=auth(),
        )
        assert client.get("/handoff/MRN-1", headers=auth()).json()["note"][
            "encounter_id"
        ] == "enc-9"

    def test_retries_do_not_duplicate_the_note(self, flow):
        """Append-only plus a flaky network would otherwise file the same
        handoff twice, leaving a spurious 'version' in the history."""
        client, repo = flow
        headers = {**auth(), "Idempotency-Key": "handoff-retry-1"}
        first = client.post("/handoff/MRN-1", json={"text": SBAR}, headers=headers)
        second = client.post("/handoff/MRN-1", json={"text": SBAR}, headers=headers)

        assert first.status_code == 201
        assert second.headers.get("Idempotent-Replayed") == "true"
        # Only one note was actually filed...
        assert len(repo.handoff_store) == 1
        # ...and the replay describes that same note.
        for field in ("patient_id", "text", "author"):
            assert second.json()["note"][field] == first.json()["note"][field]

    def test_a_retry_does_not_add_a_history_version(self, flow):
        client, _ = flow
        headers = {**auth(), "Idempotency-Key": "handoff-retry-2"}
        client.post("/handoff/MRN-1", json={"text": SBAR}, headers=headers)
        client.post("/handoff/MRN-1", json={"text": SBAR}, headers=headers)
        assert client.get("/handoff/MRN-1/history", headers=auth()).json()["count"] == 1


class TestAppendOnly:
    def test_a_new_version_supersedes_without_destroying(self, flow):
        """'What was I told at 07:00?' must survive a 09:00 edit."""
        client, _ = flow
        client.post("/handoff/MRN-1", json={"text": "0700 handoff text"}, headers=auth())
        client.post(
            "/handoff/MRN-1", json={"text": "0900 revised text"}, headers=auth("dr.day")
        )

        assert client.get("/handoff/MRN-1", headers=auth()).json()["note"]["text"] == (
            "0900 revised text"
        )

        history = client.get("/handoff/MRN-1/history", headers=auth()).json()
        assert history["count"] == 2
        assert [v["text"] for v in history["versions"]] == [
            "0900 revised text",
            "0700 handoff text",
        ]

    def test_history_records_who_wrote_each_version(self, flow):
        client, _ = flow
        client.post("/handoff/MRN-1", json={"text": "first version"}, headers=auth("dr.a"))
        client.post("/handoff/MRN-1", json={"text": "second version"}, headers=auth("dr.b"))
        history = client.get("/handoff/MRN-1/history", headers=auth()).json()
        assert [v["author"] for v in history["versions"]] == ["dr.b", "dr.a"]

    def test_history_is_bounded(self, flow):
        client, _ = flow
        for i in range(5):
            client.post("/handoff/MRN-1", json={"text": f"version {i}"}, headers=auth())
        history = client.get(
            "/handoff/MRN-1/history", params={"limit": 2}, headers=auth()
        ).json()
        assert history["count"] == 2

    def test_history_limit_is_capped(self, flow):
        client, _ = flow
        assert (
            client.get(
                "/handoff/MRN-1/history", params={"limit": 10_000}, headers=auth()
            ).status_code
            == 422
        )

    def test_empty_history_is_not_an_error(self, flow):
        client, _ = flow
        r = client.get("/handoff/MRN-NONE/history", headers=auth())
        assert r.status_code == 200
        assert r.json()["versions"] == []


class TestAuthorIsTrustworthy:
    def test_author_comes_from_the_token(self, flow):
        client, _ = flow
        client.post("/handoff/MRN-1", json={"text": SBAR}, headers=auth("dr.real"))
        assert client.get("/handoff/MRN-1", headers=auth()).json()["note"][
            "author"
        ] == "dr.real"

    def test_a_body_supplied_author_is_ignored(self, flow):
        """A note that could claim to be from another clinician is worse than
        no note at all."""
        client, _ = flow
        client.post(
            "/handoff/MRN-1",
            json={"text": SBAR, "author": "dr.someone.else"},
            headers=auth("dr.real"),
        )
        assert client.get("/handoff/MRN-1", headers=auth()).json()["note"][
            "author"
        ] == "dr.real"


class TestAccessControl:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/handoff/MRN-1"),
            ("get", "/handoff/MRN-1/history"),
            ("post", "/handoff/MRN-1"),
        ],
    )
    def test_authentication_is_required(self, flow, method, path):
        client, _ = flow
        call = getattr(client, method)
        r = call(path, json={"text": SBAR}) if method == "post" else call(path)
        assert r.status_code == 401

    def test_a_viewer_cannot_read_a_handoff(self, flow):
        """Handoff notes are clinical free text about a named patient."""
        client, _ = flow
        assert (
            client.get("/handoff/MRN-1", headers=auth(roles=["viewer"])).status_code
            == 403
        )

    def test_a_viewer_cannot_write_a_handoff(self, flow):
        client, _ = flow
        assert (
            client.post(
                "/handoff/MRN-1", json={"text": SBAR}, headers=auth(roles=["viewer"])
            ).status_code
            == 403
        )


class TestValidation:
    def test_a_blank_note_is_rejected(self, flow):
        client, _ = flow
        assert (
            client.post("/handoff/MRN-1", json={"text": "   "}, headers=auth()).status_code
            == 422
        )

    def test_an_empty_note_is_rejected(self, flow):
        client, _ = flow
        assert (
            client.post("/handoff/MRN-1", json={"text": ""}, headers=auth()).status_code
            == 422
        )

    def test_an_oversized_note_is_rejected(self, flow):
        client, _ = flow
        assert (
            client.post(
                "/handoff/MRN-1", json={"text": "x" * 10_000}, headers=auth()
            ).status_code
            == 422
        )

    def test_surrounding_whitespace_is_trimmed(self, flow):
        client, _ = flow
        client.post("/handoff/MRN-1", json={"text": f"  {SBAR}  "}, headers=auth())
        assert client.get("/handoff/MRN-1", headers=auth()).json()["note"]["text"] == SBAR

    def test_a_database_outage_is_503_not_500(self, flow):
        client, repo = flow
        repo.fail_with = PyMongoError("connection lost")
        assert client.get("/handoff/MRN-1", headers=auth()).status_code == 503
        assert (
            client.post("/handoff/MRN-1", json={"text": SBAR}, headers=auth()).status_code
            == 503
        )


class TestHandoffIsAudited:
    def test_reading_a_note_is_recorded_against_the_patient(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get("/handoff/MRN-1", headers=auth())
        record = records(caplog)[-1]
        assert record["resource_type"] == "HandoffNote"
        assert record["patient_ref"] == audit_reference("MRN-1")
        assert record["sub"] == "dr.night"

    def test_writing_a_note_is_recorded(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.post("/handoff/MRN-1", json={"text": SBAR}, headers=auth())
        record = records(caplog)[-1]
        assert record["method"] == "POST"
        assert record["patient_ref"] == audit_reference("MRN-1")

    def test_the_note_text_is_never_logged(self, flow, caplog):
        """The body is clinical free text; only its existence is auditable."""
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.post(
                "/handoff/MRN-1", json={"text": "SECRET-DIAGNOSIS-DETAIL"}, headers=auth()
            )
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "SECRET-DIAGNOSIS-DETAIL" not in blob

    def test_the_patient_id_is_not_logged_raw(self, flow, caplog):
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get("/handoff/MRN-SECRET", headers=auth())
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "MRN-SECRET" not in blob

    def test_the_history_path_is_distinguishable(self, flow, caplog):
        """A reviewer should see that someone read the whole edit history,
        not just the current note."""
        client, _ = flow
        with caplog.at_level(logging.INFO, logger="medicore.audit"):
            client.get("/handoff/MRN-1/history", headers=auth())
        assert records(caplog)[-1]["path"] == "/handoff/{id}/history"


def _run(coro):
    """Drive a coroutine on a fresh loop.

    Deliberately not the ambient loop: other modules in the suite close theirs
    during teardown, which would make these fail only when run together.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestRetention:
    def test_old_notes_are_purged(self, flow):
        """Working notes are not the medical record; retaining PHI past its
        usefulness is its own risk."""
        from datetime import UTC, datetime, timedelta

        client, repo = flow
        client.post("/handoff/MRN-1", json={"text": SBAR}, headers=auth())
        repo.handoff_store[0]["created_at"] = datetime.now(UTC) - timedelta(days=400)
        client.post("/handoff/MRN-2", json={"text": SBAR}, headers=auth())

        assert _run(repo.purge_handoffs(older_than_days=90)) == 1
        assert len(repo.handoff_store) == 1

    def test_zero_retention_never_deletes(self, flow):
        client, repo = flow
        client.post("/handoff/MRN-1", json={"text": SBAR}, headers=auth())
        assert _run(repo.purge_handoffs(older_than_days=0)) == 0
        assert len(repo.handoff_store) == 1
