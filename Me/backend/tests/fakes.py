"""Test doubles.

``FakePatientFlowRepository`` implements the same interface as the real
repository, including its concurrency semantics (unique waiting patients,
optimistic bed updates, atomic claim), so tests exercise the real handler code
without needing a MongoDB instance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.services.patient_flow.repository import ConflictError, NotFoundError


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FakePatientFlowRepository:
    def __init__(self, beds: list[dict[str, Any]] | None = None):
        self.beds_store: dict[str, dict[str, Any]] = {}
        for bed in beds or []:
            self.beds_store[bed["bed_id"]] = {
                **bed,
                "occupied": False,
                "patient_id": None,
            }
        self.queue_store: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self.ping_error: Exception | None = None

    def _guard(self) -> None:
        if self.fail_with:
            raise self.fail_with

    # -- schema ------------------------------------------------------------

    async def ensure_indexes(self) -> None:
        self._guard()

    async def seed_beds(self, beds: list[dict[str, Any]]) -> int:
        self._guard()
        created = 0
        for bed in beds:
            if bed["bed_id"] not in self.beds_store:
                self.beds_store[bed["bed_id"]] = {
                    **bed,
                    "occupied": False,
                    "patient_id": None,
                }
                created += 1
        return created

    # -- beds --------------------------------------------------------------

    async def list_beds(
        self, ward: str | None = None, occupied: bool | None = None
    ) -> list[dict[str, Any]]:
        self._guard()
        rows = list(self.beds_store.values())
        if ward:
            rows = [r for r in rows if r["ward"] == ward]
        if occupied is not None:
            rows = [r for r in rows if r["occupied"] is occupied]
        return sorted(rows, key=lambda r: (r["ward"], r["bed_id"]))

    async def get_bed(self, bed_id: str) -> dict[str, Any]:
        self._guard()
        if bed_id not in self.beds_store:
            raise NotFoundError(bed_id)
        return dict(self.beds_store[bed_id])

    async def set_bed_occupancy(
        self,
        bed_id: str,
        occupied: bool,
        patient_id: str | None,
        expected_occupied: bool | None = None,
    ) -> dict[str, Any]:
        self._guard()
        bed = self.beds_store.get(bed_id)
        if bed is None:
            raise NotFoundError(bed_id)
        if expected_occupied is not None and bed["occupied"] != expected_occupied:
            raise ConflictError(bed_id)
        bed["occupied"] = occupied
        bed["patient_id"] = patient_id if occupied else None
        bed["updated_at"] = _utcnow()
        return dict(bed)

    # -- queue -------------------------------------------------------------

    async def enqueue(
        self, patient_id: str, acuity: int, dept: str, created_by: str
    ) -> dict[str, Any]:
        self._guard()
        # Mirrors the unique partial index on (patient_id, status="waiting").
        if any(
            i["patient_id"] == patient_id and i["status"] == "waiting"
            for i in self.queue_store
        ):
            raise ConflictError(patient_id)
        doc = {
            "patient_id": patient_id,
            "acuity": acuity,
            "dept": dept,
            "status": "waiting",
            "created_at": _utcnow(),
            "created_by": created_by,
        }
        self.queue_store.append(doc)
        return dict(doc)

    def _waiting(self, dept: str | None, status: str) -> list[dict[str, Any]]:
        rows = [i for i in self.queue_store if i["status"] == status]
        if dept:
            rows = [i for i in rows if i["dept"] == dept]
        return sorted(rows, key=lambda i: (i["acuity"], i["created_at"]))

    async def list_queue(
        self, limit: int, dept: str | None = None, status: str = "waiting"
    ) -> list[dict[str, Any]]:
        self._guard()
        return [dict(i) for i in self._waiting(dept, status)[:limit]]

    async def count_queue(
        self, dept: str | None = None, status: str = "waiting"
    ) -> int:
        self._guard()
        return len(self._waiting(dept, status))

    async def claim_next(self, dept: str, clinician: str) -> dict[str, Any] | None:
        self._guard()
        candidates = self._waiting(dept, "waiting")
        if not candidates:
            return None
        item = candidates[0]
        item["status"] = "in_progress"
        item["claimed_by"] = clinician
        item["claimed_at"] = _utcnow()
        return dict(item)

    async def complete(self, patient_id: str) -> dict[str, Any]:
        self._guard()
        matches = [
            i
            for i in self.queue_store
            if i["patient_id"] == patient_id and i["status"] != "completed"
        ]
        if not matches:
            raise NotFoundError(patient_id)
        item = sorted(matches, key=lambda i: i["created_at"])[-1]
        item["status"] = "completed"
        item["completed_at"] = _utcnow()
        return dict(item)

    async def ping(self) -> None:
        if self.ping_error:
            raise self.ping_error
