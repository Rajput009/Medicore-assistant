"""MediCore patient flow: bed management and emergency department triage.

All state is persisted in MongoDB and all handlers are async, so the service
scales horizontally and does not block the event loop on database I/O.

Every route touching patient data requires the clinician or admin role.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from pymongo.errors import PyMongoError

from backend.common.app import create_service_app
from backend.common.config import settings
from backend.common.deps import (
    Principal,
    clinical_staff,
    require_department_access,
    require_ward_access,
)
from backend.common.idempotency import (
    extract_idempotency_key,
    lookup as idem_lookup,
    replay_response,
    store as idem_store,
)

from .repository import (
    ConflictError,
    NotFoundError,
    PatientFlowRepository,
    create_client,
)

logger = logging.getLogger(__name__)

_client = None
_repository: PatientFlowRepository | None = None


def get_repository() -> PatientFlowRepository:
    """FastAPI dependency; overridable in tests."""
    if _repository is None:  # pragma: no cover - defensive
        raise RuntimeError("Repository not initialised")
    return _repository


def build_bed_documents() -> list[dict[str, Any]]:
    """Deterministic bed identifiers from the configured ward layout.

    Stable ids matter: they must be identical across replicas and restarts, so
    they are derived from the layout rather than generated randomly.
    """
    docs: list[dict[str, Any]] = []
    for ward, count in settings.parsed_bed_layout:
        for index in range(1, count + 1):
            docs.append({"bed_id": f"{ward}-{index:03d}", "ward": ward})
    return docs


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _repository

    # A repository may be injected before startup (tests, or an embedding
    # process). Only open our own connection when one was not supplied, but
    # always run schema setup so beds exist either way.
    owns_client = _repository is None
    if owns_client:
        _client = create_client()
        _repository = PatientFlowRepository(_client[settings.mongo_db])

    try:
        await _repository.ensure_indexes()
        created = await _repository.seed_beds(build_bed_documents())
        if created:
            logger.info("seeded %d beds", created)
    except Exception:
        # Do not crash-loop the pod: readiness reports not-ready until the
        # database recovers, and setup is re-attempted on the next start.
        logger.exception("startup database initialisation failed")

    try:
        yield
    finally:
        if owns_client and _client is not None:
            _client.close()
            _client = None
            _repository = None


app = create_service_app(
    title="MediCore Patient Flow",
    service_name="patient-flow",
    version="1.0.0",
    lifespan=lifespan,
)

ClinicalUser = Annotated[Principal, Depends(clinical_staff)]
Repo = Annotated[PatientFlowRepository, Depends(get_repository)]


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


class Health(BaseModel):
    status: str
    service: str
    env: str


@app.get("/health", response_model=Health, tags=["ops"])
def health() -> Health:
    """Liveness: the process is up. Must not touch dependencies.

    A liveness probe that checks the database would restart healthy pods during
    a database outage, turning a degradation into an outage.
    """
    return Health(status="ok", service="patient-flow", env=settings.env)


@app.get("/ready", tags=["ops"])
async def ready(response: Response) -> dict[str, Any]:
    """Readiness: can this pod serve traffic? Verifies the database."""
    if _repository is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting", "database": "unavailable"}
    try:
        await _repository.ping()
    except Exception:
        logger.warning("readiness check failed", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": "unavailable"}
    return {"status": "ok", "database": "ok"}


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

_IDENTIFIER = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")


class Bed(BaseModel):
    bed_id: str
    ward: str
    occupied: bool
    patient_id: str | None = None


class BedUpdate(BaseModel):
    occupied: bool
    patient_id: str | None = Field(default=None, max_length=64)
    # When set, the update only applies if occupancy still has this value.
    expected_occupied: bool | None = None

    @field_validator("patient_id")
    @classmethod
    def _validate_patient(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("patient_id cannot be blank")
        return v.strip() if v else None


class QueueItem(BaseModel):
    patient_id: str = _IDENTIFIER
    # ESI acuity: 1 = most urgent, 5 = least.
    acuity: int = Field(..., ge=1, le=5)
    dept: str = _IDENTIFIER


# --------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------


def _unavailable(exc: Exception) -> HTTPException:
    # Never surface driver internals (they can contain hostnames/credentials).
    logger.error("database error", exc_info=exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Patient flow storage is temporarily unavailable",
    )


# --------------------------------------------------------------------------
# Beds
# --------------------------------------------------------------------------


@app.get("/beds", response_model=list[Bed], tags=["beds"])
async def list_beds(
    repo: Repo,
    principal: ClinicalUser,
    ward: str | None = Query(default=None, max_length=64),
    occupied: bool | None = Query(default=None),
) -> list[Bed]:
    require_ward_access(ward, principal)
    try:
        docs = await repo.list_beds(ward=ward, occupied=occupied)
    except PyMongoError as exc:
        raise _unavailable(exc) from exc
    # If the caller is ward-scoped and asked for all wards, filter in process.
    if principal.wards and "admin" not in principal.roles and ward is None:
        docs = [d for d in docs if d.get("ward") in principal.wards]
    return [Bed(**d) for d in docs]


@app.get("/beds/{bed_id}", response_model=Bed, tags=["beds"])
async def get_bed(bed_id: str, repo: Repo, principal: ClinicalUser) -> Bed:
    try:
        return Bed(**await repo.get_bed(bed_id))
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"bed {bed_id} not found") from exc
    except PyMongoError as exc:
        raise _unavailable(exc) from exc


@app.patch("/beds/{bed_id}", response_model=None, tags=["beds"])
async def update_bed(
    bed_id: str,
    update: BedUpdate,
    repo: Repo,
    principal: ClinicalUser,
    request: Request,
):
    """Assign or release a bed.

    Pass ``expected_occupied`` to make the write conditional; a 409 then means
    another clinician changed the bed first, rather than silently overwriting.

    Optional ``Idempotency-Key`` header: retries return the first successful body.
    """
    if update.occupied and not update.patient_id:
        raise HTTPException(422, "patient_id is required when marking a bed occupied")

    # Ward scope: load bed first when principal is restricted.
    if principal.wards and "admin" not in principal.roles:
        try:
            existing = await repo.get_bed(bed_id)
        except NotFoundError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"bed {bed_id} not found"
            ) from exc
        except PyMongoError as exc:
            raise _unavailable(exc) from exc
        require_ward_access(existing.get("ward"), principal)

    route = f"PATCH /beds/{bed_id}"
    idem_key = extract_idempotency_key(request)
    if idem_key:
        hit = idem_lookup(principal.sub, route, idem_key)
        if hit is not None:
            return replay_response(hit[0], hit[1])

    try:
        doc = await repo.set_bed_occupancy(
            bed_id,
            occupied=update.occupied,
            patient_id=update.patient_id,
            expected_occupied=update.expected_occupied,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"bed {bed_id} not found") from exc
    except ConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Bed was modified by another user; reload and retry",
        ) from exc
    except PyMongoError as exc:
        raise _unavailable(exc) from exc

    body = Bed(**doc).model_dump()
    if idem_key:
        idem_store(principal.sub, route, idem_key, 200, body)
    return body


# --------------------------------------------------------------------------
# Triage queue
# --------------------------------------------------------------------------


@app.post("/queue", status_code=status.HTTP_201_CREATED, response_model=None, tags=["queue"])
async def enqueue(
    item: QueueItem,
    repo: Repo,
    principal: ClinicalUser,
    request: Request,
):
    require_department_access(item.dept, principal)

    route = "POST /queue"
    idem_key = extract_idempotency_key(request)
    if idem_key:
        hit = idem_lookup(principal.sub, route, idem_key)
        if hit is not None:
            return replay_response(hit[0], hit[1])

    try:
        doc = await repo.enqueue(
            item.patient_id, item.acuity, item.dept, created_by=principal.sub
        )
    except ConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Patient {item.patient_id} is already waiting in the queue",
        ) from exc
    except PyMongoError as exc:
        raise _unavailable(exc) from exc

    body = {"ok": True, "item": doc}
    if idem_key:
        idem_store(principal.sub, route, idem_key, 201, body)
    return body


@app.get("/queue", tags=["queue"])
async def list_queue(
    repo: Repo,
    principal: ClinicalUser,
    limit: int = Query(default=25, ge=1, le=200),
    dept: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    require_department_access(dept, principal)
    try:
        items = await repo.list_queue(limit=limit, dept=dept)
        total = await repo.count_queue(dept=dept)
    except PyMongoError as exc:
        raise _unavailable(exc) from exc
    if principal.departments and "admin" not in principal.roles and dept is None:
        items = [i for i in items if i.get("dept") in principal.departments]
        total = len(items)
    return {"items": items, "count": len(items), "total": total}


@app.post("/queue/claim", response_model=None, tags=["queue"])
async def claim_next(
    repo: Repo,
    principal: ClinicalUser,
    request: Request,
    dept: str = Query(..., max_length=64),
):
    """Atomically claim the most urgent waiting patient in a department."""
    require_department_access(dept, principal)

    route = f"POST /queue/claim?dept={dept}"
    idem_key = extract_idempotency_key(request)
    if idem_key:
        hit = idem_lookup(principal.sub, route, idem_key)
        if hit is not None:
            return replay_response(hit[0], hit[1])

    try:
        doc = await repo.claim_next(dept=dept, clinician=principal.sub)
    except PyMongoError as exc:
        raise _unavailable(exc) from exc
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No patients waiting in {dept}")
    body = {"ok": True, "item": doc}
    if idem_key:
        idem_store(principal.sub, route, idem_key, 200, body)
    return body


@app.post("/queue/{patient_id}/complete", tags=["queue"])
async def complete(
    patient_id: str, repo: Repo, principal: ClinicalUser
) -> dict[str, Any]:
    try:
        doc = await repo.complete(patient_id)
    except NotFoundError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No active queue entry for {patient_id}"
        ) from exc
    except PyMongoError as exc:
        raise _unavailable(exc) from exc
    return {"ok": True, "item": doc}
