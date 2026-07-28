"""MediCore patient flow: bed management and emergency department triage.

All state is persisted in MongoDB and all handlers are async, so the service
scales horizontally and does not block the event loop on database I/O.

Every route touching patient data requires the clinician or admin role.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo.errors import PyMongoError

from backend.common import audit_store
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
    replay_response,
)
from backend.common.idempotency import (
    lookup as idem_lookup,
)
from backend.common.idempotency import (
    store as idem_store,
)
from backend.common.middleware import set_audit_sink

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

    # Bed and triage access is PHI access, and the browser reaches this
    # service directly (ingress routes /flow/* here, not through the gateway),
    # so without its own sink every bed assignment and triage claim — including
    # break-glass overrides — would be missing from audit search.
    if settings.audit_index_enabled:
        try:
            await audit_store.start()
            set_audit_sink(audit_store.submit)
        except Exception as exc:
            logger.warning(
                "audit index unavailable; audit trail remains in the log stream",
                extra={"error_type": type(exc).__name__, "error": str(exc)[:200]},
            )

    try:
        yield
    finally:
        set_audit_sink(None)
        await audit_store.stop()
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


# Closed set, because free text cannot be reported on. Each value is one an
# emergency department is measured on.
DISPOSITIONS: frozenset[str] = frozenset(
    {
        "admitted",
        "discharged",
        "transferred",
        # Deliberately its own value rather than folded into "other": it is
        # the outcome a department is most accountable for and must be
        # countable without parsing prose.
        "left_without_being_seen",
        "deceased",
        "other",
    }
)

# A disposition that says nothing on its own needs a note to be reviewable.
DISPOSITIONS_REQUIRING_NOTE: frozenset[str] = frozenset(
    {"left_without_being_seen", "other"}
)

# Below this, a reason adds nothing but keystrokes. At or above this urgency
# (numerically <=), the escalation needs a justification someone can review.
REASON_REQUIRED_AT_ACUITY = 2
MIN_REASON_LENGTH = 10
MAX_REASON_LENGTH = 500
MAX_NOTE_LENGTH = 500


class VitalsSnapshot(BaseModel):
    """The vitals behind a NEWS2 score, copied at escalation time.

    Bounds mirror the CDS service so a value that could not be scored can
    never be recorded as the justification for an escalation.
    """

    model_config = {"extra": "forbid"}

    respiratory_rate: float | None = Field(default=None, gt=0, le=80)
    spo2: float | None = Field(default=None, ge=1, le=100)
    temperature: float | None = Field(default=None, ge=25, le=45)
    systolic_bp: float | None = Field(default=None, gt=0, le=300)
    pulse: float | None = Field(default=None, gt=0, le=300)
    consciousness: str | None = Field(default=None, max_length=1)


class QueueItem(BaseModel):
    patient_id: str = _IDENTIFIER
    # ESI acuity: 1 = most urgent, 5 = least.
    acuity: int = Field(..., ge=1, le=5)
    dept: str = _IDENTIFIER

    # --- Why this patient is being escalated -----------------------------
    # Optional in the model, conditionally required in the validator: a
    # clinician must be able to escalate on judgement alone, but an urgent
    # escalation with no stated reason is unreviewable.
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    news2_score: int | None = Field(default=None, ge=0, le=25)
    news2_band: str | None = Field(default=None, max_length=32)
    red_flag: bool | None = None
    vitals_snapshot: VitalsSnapshot | None = None

    @field_validator("reason")
    @classmethod
    def _reason_is_substantive(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = " ".join(v.split())
        if not cleaned:
            return None
        if len(cleaned) < MIN_REASON_LENGTH:
            raise ValueError(
                f"reason must be at least {MIN_REASON_LENGTH} characters and "
                "describe the clinical concern"
            )
        return cleaned

    @field_validator("news2_band")
    @classmethod
    def _known_band(cls, v: str | None) -> str | None:
        if v is None:
            return None
        band = v.strip().lower()
        if band not in {"low", "low-medium", "medium", "high"}:
            raise ValueError("news2_band must be low, low-medium, medium or high")
        return band

    @model_validator(mode="after")
    def _urgent_escalations_need_a_reason(self) -> QueueItem:
        if self.acuity <= REASON_REQUIRED_AT_ACUITY and not self.reason:
            raise ValueError(
                f"reason is required for acuity {self.acuity} "
                f"(mandatory at acuity {REASON_REQUIRED_AT_ACUITY} or more urgent)"
            )
        return self


class QueueCompletion(BaseModel):
    """What actually happened to the patient.

    Required, not optional: a patient leaving the queue without a recorded
    outcome is the gap this endpoint exists to close.
    """

    disposition: str = Field(..., max_length=64)
    disposition_note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)

    @field_validator("disposition")
    @classmethod
    def _known_disposition(cls, v: str) -> str:
        value = v.strip().lower()
        if value not in DISPOSITIONS:
            raise ValueError(
                "disposition must be one of: " + ", ".join(sorted(DISPOSITIONS))
            )
        return value

    @field_validator("disposition_note")
    @classmethod
    def _clean_note(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = " ".join(v.split())
        return cleaned or None

    @model_validator(mode="after")
    def _ambiguous_dispositions_need_a_note(self) -> QueueCompletion:
        if self.disposition in DISPOSITIONS_REQUIRING_NOTE and not self.disposition_note:
            raise ValueError(
                f"disposition_note is required when disposition is "
                f"'{self.disposition}'"
            )
        return self


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
    # No break-glass on list endpoints. Emergency access is for reaching a
    # specific patient right now; allowing it here would turn an override into
    # bulk browsing of every ward, which is the abuse the scope exists to stop.
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
        # Break-glass permitted: assigning a specific bed for a deteriorating
        # patient is exactly the emergency this exists for.
        require_ward_access(existing.get("ward"), principal, request)

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
    require_department_access(item.dept, principal, request)

    route = "POST /queue"
    idem_key = extract_idempotency_key(request)
    if idem_key:
        hit = idem_lookup(principal.sub, route, idem_key)
        if hit is not None:
            return replay_response(hit[0], hit[1])

    try:
        doc = await repo.enqueue(
            item.patient_id,
            item.acuity,
            item.dept,
            created_by=principal.sub,
            reason=item.reason,
            news2_score=item.news2_score,
            news2_band=item.news2_band,
            red_flag=item.red_flag,
            vitals_snapshot=(
                item.vitals_snapshot.model_dump(exclude_none=True)
                if item.vitals_snapshot
                else None
            ),
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
    # Strict by design — see the note on list_beds.
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
    require_department_access(dept, principal, request)

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


@app.get("/queue/stats", tags=["queue"])
async def queue_stats(
    repo: Repo,
    principal: ClinicalUser,
    dept: str | None = Query(default=None, max_length=64),
    since_hours: int = Query(default=24, ge=1, le=24 * 90),
) -> dict[str, Any]:
    """Counts by disposition, LWBS rate and time-to-completion percentiles.

    This is what the ward gets back for the extra keystrokes at completion.
    Without it, recording a disposition is pure data entry, and data entry
    that returns nothing to the person doing it degrades quickly.
    """
    require_department_access(dept, principal)
    since = datetime.now(UTC) - timedelta(hours=since_hours)
    try:
        stats = await repo.queue_stats(dept=dept, since=since)
    except PyMongoError as exc:
        raise _unavailable(exc) from exc
    return {
        "dept": dept,
        "since": since.isoformat(),
        "window_hours": since_hours,
        **stats,
    }


# NOTE: literal segments must be declared before "/{patient_id}" routes.
# FastAPI matches in declaration order, so a future GET /queue/{patient_id}
# declared above this would swallow /queue/stats and treat "stats" as a
# patient id. Guarded by test_queue_outcomes.py::test_stats_route_is_not_shadowed.
@app.post("/queue/{patient_id}/complete", response_model=None, tags=["queue"])
async def complete(
    patient_id: str,
    completion: QueueCompletion,
    repo: Repo,
    principal: ClinicalUser,
    request: Request,
):
    """Close a triage entry with a record of what happened to the patient.

    ``disposition`` is required. "Completed" previously meant only "removed
    from the list", which could not distinguish a patient admitted to ICU
    from one who walked out unseen — the two ends of the safety spectrum a
    department is accountable for.

    Honours ``Idempotency-Key``. Without it a retry after a lost response
    returns 404 ("no active queue entry") even though the completion
    succeeded, which reads to the clinician as if the patient vanished.
    """
    route = f"POST /queue/{patient_id}/complete"
    idem_key = extract_idempotency_key(request)
    if idem_key:
        hit = idem_lookup(principal.sub, route, idem_key)
        if hit is not None:
            return replay_response(hit[0], hit[1])

    try:
        doc = await repo.complete(
            patient_id,
            disposition=completion.disposition,
            completed_by=principal.sub,
            disposition_note=completion.disposition_note,
        )
    except ConflictError as exc:
        # Already closed. Refusing protects the recorded outcome from being
        # quietly rewritten; a genuine correction is a separate action.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Queue entry for {patient_id} is already completed",
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No active queue entry for {patient_id}"
        ) from exc
    except PyMongoError as exc:
        raise _unavailable(exc) from exc

    body = {"ok": True, "item": doc}
    if idem_key:
        idem_store(principal.sub, route, idem_key, 200, body)
    return body


@app.get("/queue/{patient_id}/history", tags=["queue"])
async def queue_history(
    patient_id: str,
    repo: Repo,
    principal: ClinicalUser,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Every queue entry for a patient, newest first.

    A patient can pass through triage more than once; the escalation evidence
    and the disposition are only useful if the whole sequence is retrievable.
    """
    try:
        entries = await repo.queue_history(patient_id, limit=limit)
    except PyMongoError as exc:
        raise _unavailable(exc) from exc
    return {"patient_id": patient_id, "entries": entries, "count": len(entries)}


# --------------------------------------------------------------------------
# Handoff notes (SBAR)
#
# Shift-handoff notes lived in the browser's sessionStorage, which meant a
# clinician who closed the tab lost the handoff they had just written, and the
# incoming shift could not read it at all — the one thing a handoff note is
# for. These persist them server-side.
#
# Storage choice: patient-flow's Mongo, not a FHIR ``Communication``.
#   * These are *working notes*, not the record of truth. Writing unverified
#     free text into the hospital EHR would put it in the legal record and in
#     front of every other system that reads from it.
#   * The gateway's only FHIR write is coded Observations, deliberately narrow
#     so what was written is always auditable. Free text does not fit that.
#   * They belong to the same shift-workflow state (beds, triage) this service
#     already owns.
# Promoting a note into the EHR is a separate, explicit action — and should
# stay one.
#
# Append-only: "what was I told at 07:00?" must remain answerable after
# someone edits the note at 09:00.
# --------------------------------------------------------------------------


MAX_HANDOFF_LENGTH = 4000


class HandoffWrite(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_HANDOFF_LENGTH)
    encounter_id: str | None = Field(default=None, max_length=64)

    @field_validator("text")
    @classmethod
    def _not_only_whitespace(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Handoff note cannot be blank")
        return cleaned


@app.get("/handoff/{patient_id}", tags=["handoff"])
async def get_handoff(
    patient_id: str, repo: Repo, principal: ClinicalUser
) -> dict[str, Any]:
    """The current handoff note for a patient, or null when none exists."""
    try:
        note = await repo.latest_handoff(patient_id)
    except PyMongoError as exc:
        raise _unavailable(exc) from exc
    return {"patient_id": patient_id, "note": note}


@app.get("/handoff/{patient_id}/history", tags=["handoff"])
async def get_handoff_history(
    patient_id: str,
    repo: Repo,
    principal: ClinicalUser,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Every version, newest first.

    Append-only storage is only useful if the earlier versions can actually be
    read back; this is that view.
    """
    try:
        versions = await repo.handoff_history(patient_id, limit=limit)
    except PyMongoError as exc:
        raise _unavailable(exc) from exc
    return {"patient_id": patient_id, "versions": versions, "count": len(versions)}


@app.post("/handoff/{patient_id}", status_code=status.HTTP_201_CREATED, tags=["handoff"])
async def save_handoff(
    patient_id: str,
    payload: HandoffWrite,
    repo: Repo,
    principal: ClinicalUser,
    request: Request,
) -> dict[str, Any]:
    """Append a new version of the handoff note.

    The author is taken from the verified token, never from the body: a note
    that could claim to be from another clinician is worse than no note.
    """
    route = f"POST /handoff/{patient_id}"
    idem_key = extract_idempotency_key(request)
    if idem_key:
        hit = idem_lookup(principal.sub, route, idem_key)
        if hit is not None:
            return replay_response(hit[0], hit[1])

    try:
        note = await repo.add_handoff(
            patient_id,
            payload.text,
            author=principal.sub,
            encounter_id=payload.encounter_id,
        )
    except PyMongoError as exc:
        raise _unavailable(exc) from exc

    body = {"ok": True, "note": note}
    if idem_key:
        idem_store(principal.sub, route, idem_key, 201, body)
    return body
