"""MediCore patient flow: bed management + triage queue."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

from backend.common.config import settings
from backend.common.middleware import AuditLogMiddleware
from backend.common.telemetry import instrument_fastapi

app = instrument_fastapi(
    FastAPI(title="MediCore Patient Flow", version="0.1.0"),
    service_name="patient-flow",
)
app.add_middleware(AuditLogMiddleware)


class Health(BaseModel):
    status: str
    service: str
    env: str


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", service="patient-flow", env=settings.env)


# --------------------------------------------------------------------------
# Beds (in-memory stub)
# --------------------------------------------------------------------------


class Bed(BaseModel):
    id: str
    ward: str
    occupied: bool


BEDS: list[Bed] = [
    Bed(id=str(uuid4()), ward="A", occupied=False) for _ in range(4)
]


@app.get("/beds", response_model=list[Bed])
def list_beds(ward: str | None = None) -> list[Bed]:
    if ward:
        return [b for b in BEDS if b.ward == ward]
    return BEDS


@app.patch("/beds/{bed_id}", response_model=Bed)
def update_bed(bed_id: str, occupied: bool) -> Bed:
    for bed in BEDS:
        if bed.id == bed_id:
            bed.occupied = occupied
            return bed
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"bed {bed_id} not found"
    )


# --------------------------------------------------------------------------
# Triage queue (MongoDB)
# --------------------------------------------------------------------------

# serverSelectionTimeoutMS keeps requests from hanging ~30s when Mongo is down.
_client = MongoClient(
    settings.mongo_uri,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    tz_aware=True,
)
mdb = _client[settings.mongo_db]
_queue = mdb.triage_queue


@app.on_event("startup")
def _ensure_indexes() -> None:
    try:
        # Supports the sorted queue read below.
        _queue.create_index([("acuity", ASCENDING), ("created_at", ASCENDING)])
        _queue.create_index([("patient_id", ASCENDING)])
    except PyMongoError:
        # Don't block startup if Mongo is briefly unavailable.
        pass


@app.on_event("shutdown")
def _close_mongo() -> None:
    _client.close()


class QueueItem(BaseModel):
    patient_id: str = Field(..., min_length=1)
    # ESI-style acuity: 1 = most urgent, 5 = least.
    acuity: int = Field(..., ge=1, le=5)
    dept: str = Field(..., min_length=1)


@app.post("/queue", status_code=status.HTTP_201_CREATED)
def enqueue(item: QueueItem) -> dict[str, Any]:
    rec = item.model_dump()
    rec["created_at"] = datetime.now(UTC)
    try:
        result = _queue.insert_one(rec)
    except PyMongoError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {exc}",
        ) from exc
    return {"ok": True, "id": str(result.inserted_id)}


@app.get("/queue")
def list_queue(
    limit: int = Query(default=10, ge=1, le=200),
    dept: str | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if dept:
        query["dept"] = dept
    try:
        # Most urgent first, then longest waiting.
        cursor = (
            _queue.find(query, {"_id": 0})
            .sort([("acuity", ASCENDING), ("created_at", ASCENDING)])
            .limit(limit)
        )
        items = list(cursor)
    except PyMongoError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {exc}",
        ) from exc
    return {"items": items, "count": len(items)}
