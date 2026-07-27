"""Test doubles.

``FakePatientFlowRepository`` implements the same interface as the real
repository, including its concurrency semantics (unique waiting patients,
optimistic bed updates, atomic claim), so tests exercise the real handler code
without needing a MongoDB instance.
"""

from __future__ import annotations

import math
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
        # Append-only, mirroring the real collection.
        self.handoff_store: list[dict[str, Any]] = []
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
        self,
        patient_id: str,
        acuity: int,
        dept: str,
        created_by: str,
        *,
        reason: str | None = None,
        news2_score: int | None = None,
        news2_band: str | None = None,
        red_flag: bool | None = None,
        vitals_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._guard()
        # Mirrors the unique partial index on (patient_id, status="waiting").
        if any(
            i["patient_id"] == patient_id and i["status"] == "waiting"
            for i in self.queue_store
        ):
            raise ConflictError(patient_id)
        doc: dict[str, Any] = {
            "patient_id": patient_id,
            "acuity": acuity,
            "dept": dept,
            "status": "waiting",
            "created_at": _utcnow(),
            "created_by": created_by,
        }
        # Same rule as the real repository: absent evidence is omitted, never
        # written as an explicit null.
        if reason:
            doc["reason"] = reason
        if news2_score is not None:
            doc["news2_score"] = news2_score
        if news2_band:
            doc["news2_band"] = news2_band
        if red_flag is not None:
            doc["red_flag"] = red_flag
        if vitals_snapshot:
            doc["vitals_snapshot"] = vitals_snapshot
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

    async def complete(
        self,
        patient_id: str,
        *,
        disposition: str,
        completed_by: str,
        disposition_note: str | None = None,
    ) -> dict[str, Any]:
        self._guard()
        matches = [
            i
            for i in self.queue_store
            if i["patient_id"] == patient_id and i["status"] != "completed"
        ]
        if not matches:
            # Distinguish "already closed" from "never existed", as the real
            # repository does.
            if any(i["patient_id"] == patient_id for i in self.queue_store):
                raise ConflictError(patient_id)
            raise NotFoundError(patient_id)
        item = sorted(matches, key=lambda i: i["created_at"])[-1]
        completed_at = _utcnow()
        item["status"] = "completed"
        item["completed_at"] = completed_at
        item["disposition"] = disposition
        item["completed_by"] = completed_by
        if disposition_note:
            item["disposition_note"] = disposition_note
        created_at = item.get("created_at")
        if isinstance(created_at, datetime):
            item["time_to_completion_seconds"] = round(
                max(0.0, (completed_at - created_at).total_seconds()), 3
            )
        return dict(item)

    async def queue_history(self, patient_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self._guard()
        rows = [i for i in self.queue_store if i["patient_id"] == patient_id]
        return [dict(i) for i in sorted(rows, key=lambda i: i["created_at"], reverse=True)[:limit]]

    async def queue_stats(
        self, dept: str | None = None, since: datetime | None = None
    ) -> dict[str, Any]:
        self._guard()
        rows = [i for i in self.queue_store if i["status"] == "completed"]
        if dept:
            rows = [i for i in rows if i.get("dept") == dept]
        if since:
            rows = [i for i in rows if i.get("created_at") and i["created_at"] >= since]

        by_disposition: dict[str, int] = {}
        durations: list[float] = []
        for row in rows:
            key = str(row.get("disposition") or "unrecorded")
            by_disposition[key] = by_disposition.get(key, 0) + 1
            created_at, completed_at = row.get("created_at"), row.get("completed_at")
            if isinstance(created_at, datetime) and isinstance(completed_at, datetime):
                durations.append(max(0.0, (completed_at - created_at).total_seconds()))

        waiting = [i for i in self.queue_store if i["status"] == "waiting"]
        if dept:
            waiting = [i for i in waiting if i.get("dept") == dept]

        total = len(rows)
        lwbs = by_disposition.get("left_without_being_seen", 0)

        def pct(values: list[float], p: int) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            rank = max(1, math.ceil(p / 100 * len(ordered)))
            return round(ordered[min(rank, len(ordered)) - 1], 3)

        return {
            "completed": total,
            "waiting": len(waiting),
            "by_disposition": by_disposition,
            "left_without_being_seen_rate": round(lwbs / total, 4) if total else 0.0,
            "median_seconds": pct(durations, 50),
            "p90_seconds": pct(durations, 90),
        }

    # -- handoff notes -----------------------------------------------------

    async def add_handoff(
        self,
        patient_id: str,
        text: str,
        author: str,
        *,
        encounter_id: str | None = None,
    ) -> dict[str, Any]:
        self._guard()
        doc = {
            "patient_id": patient_id,
            "text": text,
            "author": author,
            "encounter_id": encounter_id,
            # Monotonic within a test even when writes land in the same tick,
            # so "newest wins" is deterministic rather than clock-dependent.
            "created_at": _utcnow(),
            "_seq": len(self.handoff_store),
        }
        self.handoff_store.append(doc)
        return {k: v for k, v in doc.items() if k != "_seq"}

    def _handoffs_for(self, patient_id: str) -> list[dict[str, Any]]:
        rows = [n for n in self.handoff_store if n["patient_id"] == patient_id]
        return sorted(rows, key=lambda n: n["_seq"], reverse=True)

    async def latest_handoff(self, patient_id: str) -> dict[str, Any] | None:
        self._guard()
        rows = self._handoffs_for(patient_id)
        if not rows:
            return None
        return {k: v for k, v in rows[0].items() if k != "_seq"}

    async def handoff_history(
        self, patient_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        self._guard()
        return [
            {k: v for k, v in n.items() if k != "_seq"}
            for n in self._handoffs_for(patient_id)[:limit]
        ]

    async def purge_handoffs(self, older_than_days: int) -> int:
        self._guard()
        if older_than_days <= 0:
            return 0
        from datetime import timedelta

        cutoff = _utcnow() - timedelta(days=older_than_days)
        before = len(self.handoff_store)
        self.handoff_store = [
            n for n in self.handoff_store if n["created_at"] >= cutoff
        ]
        return before - len(self.handoff_store)

    async def ping(self) -> None:
        if self.ping_error:
            raise self.ping_error
