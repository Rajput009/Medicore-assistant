"""Persistence for patient flow.

All state lives in MongoDB, not in process memory: the deployment runs multiple
replicas, so in-process state would mean each pod serves a different view of
the ward and every write would be lost on restart or reschedule.

Motor (async) is used rather than PyMongo because the request handlers run on
the event loop — a synchronous driver would block it for the duration of every
query, serialising all concurrent requests behind the slowest one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.common.config import settings


class ConflictError(RuntimeError):
    """Raised when a write loses an optimistic-concurrency check."""


class NotFoundError(LookupError):
    """Raised when the target document does not exist."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _strip_id(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop Mongo's ObjectId, which is not JSON-serialisable.

    Applied in Python rather than via find_one_and_update's ``projection``
    keyword: that parameter is positional in the driver signature and passing
    it by keyword is not portable across driver versions.
    """
    return {k: v for k, v in doc.items() if k != "_id"}


class PatientFlowRepository:
    """Data access for beds and the triage queue."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.beds = db.beds
        self.queue = db.triage_queue

    # -- schema ------------------------------------------------------------

    async def ensure_indexes(self) -> None:
        """Create indexes. Safe to call on every start; Mongo is idempotent."""
        await self.beds.create_index([("bed_id", ASCENDING)], unique=True)
        await self.beds.create_index([("ward", ASCENDING), ("occupied", ASCENDING)])
        # A patient may appear in the queue only once while still waiting.
        await self.queue.create_index(
            [("patient_id", ASCENDING)],
            unique=True,
            partialFilterExpression={"status": "waiting"},
        )
        await self.queue.create_index(
            [("status", ASCENDING), ("acuity", ASCENDING), ("created_at", ASCENDING)]
        )
        await self.queue.create_index([("dept", ASCENDING), ("status", ASCENDING)])

    async def seed_beds(self, beds: list[dict[str, Any]]) -> int:
        """Insert any missing beds. Existing rows are left untouched.

        Uses upsert-if-absent so concurrently starting replicas cannot create
        duplicates and cannot clobber live occupancy.
        """
        created = 0
        for bed in beds:
            result = await self.beds.update_one(
                {"bed_id": bed["bed_id"]},
                {
                    "$setOnInsert": {
                        **bed,
                        "occupied": False,
                        "patient_id": None,
                        "updated_at": utcnow(),
                    }
                },
                upsert=True,
            )
            if result.upserted_id is not None:
                created += 1
        return created

    # -- beds --------------------------------------------------------------

    async def list_beds(
        self, ward: str | None = None, occupied: bool | None = None
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if ward:
            query["ward"] = ward
        if occupied is not None:
            query["occupied"] = occupied
        cursor = self.beds.find(query, {"_id": 0}).sort(
            [("ward", ASCENDING), ("bed_id", ASCENDING)]
        )
        return [doc async for doc in cursor]

    async def get_bed(self, bed_id: str) -> dict[str, Any]:
        doc = await self.beds.find_one({"bed_id": bed_id}, {"_id": 0})
        if doc is None:
            raise NotFoundError(bed_id)
        return doc

    async def set_bed_occupancy(
        self,
        bed_id: str,
        occupied: bool,
        patient_id: str | None,
        expected_occupied: bool | None = None,
    ) -> dict[str, Any]:
        """Update occupancy atomically.

        ``expected_occupied`` enables optimistic concurrency: two clinicians
        assigning the same bed simultaneously must not both succeed, or two
        patients end up in one bed.
        """
        query: dict[str, Any] = {"bed_id": bed_id}
        if expected_occupied is not None:
            query["occupied"] = expected_occupied

        doc = await self.beds.find_one_and_update(
            query,
            {
                "$set": {
                    "occupied": occupied,
                    "patient_id": patient_id if occupied else None,
                    "updated_at": utcnow(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            return _strip_id(doc)

        # Distinguish "no such bed" from "someone got there first".
        if await self.beds.count_documents({"bed_id": bed_id}, limit=1) == 0:
            raise NotFoundError(bed_id)
        raise ConflictError(bed_id)

    # -- triage queue ------------------------------------------------------

    async def enqueue(
        self, patient_id: str, acuity: int, dept: str, created_by: str
    ) -> dict[str, Any]:
        doc = {
            "patient_id": patient_id,
            "acuity": acuity,
            "dept": dept,
            "status": "waiting",
            "created_at": utcnow(),
            "created_by": created_by,
        }
        try:
            await self.queue.insert_one(dict(doc))
        except DuplicateKeyError as exc:
            raise ConflictError(patient_id) from exc
        doc.pop("_id", None)
        return doc

    async def list_queue(
        self, limit: int, dept: str | None = None, status: str = "waiting"
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"status": status}
        if dept:
            query["dept"] = dept
        cursor = (
            self.queue.find(query, {"_id": 0})
            # Most urgent first, then longest waiting.
            .sort([("acuity", ASCENDING), ("created_at", ASCENDING)]).limit(limit)
        )
        return [doc async for doc in cursor]

    async def count_queue(self, dept: str | None = None, status: str = "waiting") -> int:
        query: dict[str, Any] = {"status": status}
        if dept:
            query["dept"] = dept
        return await self.queue.count_documents(query)

    async def claim_next(self, dept: str, clinician: str) -> dict[str, Any] | None:
        """Atomically take the most urgent waiting patient.

        find_one_and_update is a single atomic operation, so two clinicians
        calling this concurrently can never receive the same patient.
        """
        doc = await self.queue.find_one_and_update(
            {"status": "waiting", "dept": dept},
            {
                "$set": {
                    "status": "in_progress",
                    "claimed_by": clinician,
                    "claimed_at": utcnow(),
                }
            },
            sort=[("acuity", ASCENDING), ("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return _strip_id(doc) if doc is not None else None

    async def complete(self, patient_id: str) -> dict[str, Any]:
        doc = await self.queue.find_one_and_update(
            {"patient_id": patient_id, "status": {"$ne": "completed"}},
            {"$set": {"status": "completed", "completed_at": utcnow()}},
            sort=[("created_at", DESCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise NotFoundError(patient_id)
        return _strip_id(doc)

    async def ping(self) -> None:
        """Raise if the database is not reachable."""
        await self.db.command("ping")


def create_client() -> AsyncIOMotorClient:
    """Build the Mongo client with bounded timeouts and a real pool.

    Without explicit timeouts the driver waits ~30s to select a server, which
    turns a database blip into cascading request timeouts upstream.
    """
    return AsyncIOMotorClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
        connectTimeoutMS=settings.mongo_connect_timeout_ms,
        socketTimeoutMS=settings.mongo_socket_timeout_ms,
        maxPoolSize=settings.mongo_max_pool_size,
        minPoolSize=settings.mongo_min_pool_size,
        retryWrites=True,
        tz_aware=True,
    )
