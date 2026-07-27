"""MediCore API gateway.

Edge service: enforces JWT auth + RBAC and proxies FHIR reads/searches with a
Postgres-backed response cache.
"""

import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from backend.common import audit_store
from backend.common.app import create_service_app
from backend.common.cache import (
    close_pool,
    get_cached,
    init_pool,
    invalidate_cache,
    set_cached,
    start_janitor,
    stop_janitor,
)
from backend.common.cache import (
    ping as cache_ping,
)
from backend.common.config import settings
from backend.common.fhir_client import FHIRError, default_fhir_client
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
from backend.common.middleware import (
    audit_reference,
    note_audit_patient,
    patient_id_from_resource,
    set_audit_sink,
)
from backend.services.gateway.auth import User, admin_only, clinician_or_admin
from backend.services.gateway.auth_middleware import JWTAuthMiddleware
from backend.services.gateway.observations import (
    CONSCIOUSNESS_LABELS,
    build_vitals_bundle,
)

fhir = default_fhir_client()

# Cache TTLs per resource type (seconds). Observations change far more often.
CACHE_TTL = {
    "Patient": 300,
    "Encounter": 300,
    "Observation": 60,
    "MedicationRequest": 300,
    # Safety-critical context. Allergies change rarely but a stale allergy
    # list is the kind of error that harms someone, so it gets a short TTL.
    "AllergyIntolerance": 120,
    "Condition": 300,
}

# Only these resource types may be targeted by cache invalidation, so a path
# parameter can never be used to probe arbitrary values.
KNOWN_RESOURCES = frozenset(CACHE_TTL)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Create the pool (and cache table) up front so the first request does
        # not pay the setup cost, and readiness reflects reality immediately.
        await init_pool()
        await start_janitor()
    except Exception as exc:
        # Log a one-line summary, not a full traceback: this fires on every
        # start while the database is unreachable and would otherwise flood
        # the log pipeline. Readiness reports the degradation.
        logger.warning(
            "cache initialisation failed; serving without cache",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:200]},
        )

    if settings.audit_index_enabled:
        try:
            await audit_store.start()
            # Only register the sink once the writer is actually running,
            # so records are never handed to a dead queue.
            set_audit_sink(audit_store.submit)
        except Exception as exc:
            # The log stream still carries every audit record; only the
            # searchable index is unavailable. That is a degradation to
            # report, not a reason to refuse clinical traffic.
            logger.warning(
                "audit index unavailable; audit trail remains in the log stream",
                extra={"error_type": type(exc).__name__, "error": str(exc)[:200]},
            )

    try:
        yield
    finally:
        set_audit_sink(None)
        await audit_store.stop()
        await stop_janitor()
        await fhir.aclose()
        await close_pool()


# Auth is registered first so it runs innermost: the audit logger wraps it and
# still records rejected (401/403) requests — denied access is what an audit
# trail most needs.
app = create_service_app(
    title="MediCore Gateway",
    service_name="gateway",
    version="0.1.0",
    lifespan=lifespan,
    enable_cors=True,
    cors_methods=("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"),
    extra_middleware=((JWTAuthMiddleware, {}),),
)


class Health(BaseModel):
    status: str
    service: str
    env: str


@app.get("/health", response_model=Health, tags=["ops"])
def health() -> Health:
    """Liveness only: must not touch dependencies, or a database outage would
    restart otherwise-healthy pods."""
    return Health(status="ok", service="gateway", env=settings.env)


@app.get("/ready", tags=["ops"])
async def ready(response: Response) -> dict[str, Any]:
    """Readiness: verifies the cache database before accepting traffic.

    The audit index is reported but deliberately does **not** affect
    readiness: the log stream remains the system of record, so a stalled index
    is a searchability degradation, not a reason to pull a healthy pod out of
    the load balancer. This endpoint is unauthenticated, so it reports a bare
    status word — the counters live behind admin auth on /audit/stats.
    """
    audit_state = "disabled"
    if settings.audit_index_enabled:
        audit_state = "ok" if audit_store.stats()["running"] else "degraded"

    try:
        await cache_ping()
    except Exception:
        logger.warning("readiness check failed", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "degraded",
            "cache": "unavailable",
            "audit_index": audit_state,
        }
    return {"status": "ok", "cache": "ok", "audit_index": audit_state}


@app.get("/secure")
def secure_route(user: User = Depends(clinician_or_admin)) -> dict[str, Any]:
    return {"ok": True, "sub": user.sub, "roles": user.roles}


# --------------------------------------------------------------------------
# FHIR helpers
# --------------------------------------------------------------------------

# Query params that are gateway-internal / meaningless upstream.
_RESERVED_PARAMS = frozenset({"access_token", "token"})

# Only FHIR search parameters we intend to support are forwarded. An
# allow-list (rather than a deny-list) means an attacker cannot reach
# undocumented upstream behaviour, and cannot flood the response cache with
# unbounded distinct keys by varying junk parameter names.
ALLOWED_SEARCH_PARAMS: frozenset[str] = frozenset(
    {
        "patient", "subject", "identifier", "_id",
        "name", "family", "given", "birthdate", "gender",
        "status", "category", "code", "date", "encounter",
        "class", "type", "intent", "authoredon", "requester",
        "clinical-status", "verification-status", "criticality",
        "_count", "_sort", "_include", "_lastUpdated", "page",
    }
)

MAX_PARAM_VALUE_LENGTH = 128
MAX_SEARCH_PARAMS = 12
# Upper bound on the page size we will ask the FHIR server for. Without this a
# caller can request _count=999999 and force a huge upstream response.
MAX_COUNT = 100
DEFAULT_COUNT = 50


def _clean_params(request: Request) -> dict[str, str]:
    """Validate and normalise search parameters before they leave the gateway."""
    params: dict[str, str] = {}
    for key, value in request.query_params.items():
        if key in _RESERVED_PARAMS:
            continue
        if key not in ALLOWED_SEARCH_PARAMS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported search parameter '{key}'",
            )
        if len(value) > MAX_PARAM_VALUE_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Value for '{key}' exceeds {MAX_PARAM_VALUE_LENGTH} characters",
            )
        params[key] = value

    if len(params) > MAX_SEARCH_PARAMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_SEARCH_PARAMS} search parameters are supported",
        )

    # Clamp paging so one request cannot pull an unbounded amount of PHI.
    raw_count = params.get("_count")
    if raw_count is not None:
        try:
            count = int(raw_count)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="_count must be an integer",
            ) from None
        params["_count"] = str(max(1, min(count, MAX_COUNT)))
    else:
        params["_count"] = str(DEFAULT_COUNT)

    return params


# FHIR ids are constrained by the spec to this character set.
_FHIR_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _validate_id(resource_id: str) -> str:
    if not _FHIR_ID.match(resource_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed resource id",
        )
    return resource_id


async def _read(
    resource: str, resource_id: str, request: Request | None = None
) -> dict[str, Any]:
    _validate_id(resource_id)
    try:
        result = await fhir.read(resource, resource_id)
    except FHIRError as exc:
        # Preserve upstream 404s instead of masking everything as a 502.
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{resource}/{resource_id} not found",
            ) from exc
        # Never echo driver/upstream internals — they can contain URLs with
        # credentials or stack fragments.
        logger.warning(
            "upstream FHIR read failed",
            extra={
                "resource": resource,
                "status_code": exc.status_code,
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream clinical data service unavailable",
        ) from exc

    # Attribute the access to the patient the resource belongs to. Reading an
    # Observation is an access to that patient's record, but only the resource
    # body knows whose. Without this the audit trail records an opaque
    # observation id that no patient-scoped search will ever match.
    if request is not None:
        note_audit_patient(request, patient_id_from_resource(result))
    return result


async def _search(resource: str, params: dict[str, str]) -> dict[str, Any]:
    ttl = CACHE_TTL.get(resource, 300)
    try:
        cached = await get_cached(resource, params, max_age_seconds=ttl)
        if cached is not None:
            return cached
    except Exception:
        # A cache outage must not take the read path down with it.
        cached = None

    try:
        resp = await fhir.search(resource, params)
    except FHIRError as exc:
        logger.warning(
            "upstream FHIR search failed",
            extra={
                "resource": resource,
                "status_code": exc.status_code,
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream clinical data service unavailable",
        ) from exc

    try:
        await set_cached(resource, params, resp)
    except Exception:
        pass
    return resp


# --------------------------------------------------------------------------
# FHIR routes
#
# IMPORTANT: the literal "/search" routes must be declared *before* the
# "/{id}" routes. FastAPI matches in declaration order, so registering
# "/fhir/patient/{patient_id}" first would swallow "/fhir/patient/search"
# and treat "search" as a patient id.
# --------------------------------------------------------------------------


@app.get("/fhir/patient/search")
async def search_patients(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Patient", _clean_params(request))


@app.get("/fhir/patient/{patient_id}")
async def get_patient(
    patient_id: str, request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Patient", patient_id, request)


# Kept for backwards compatibility with earlier docs/clients.
@app.get("/fhir/patient/{patient_id}/protected")
async def get_patient_protected(
    patient_id: str, request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Patient", patient_id, request)


@app.get("/fhir/encounter/search")
async def search_encounters(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Encounter", _clean_params(request))


@app.get("/fhir/encounter/{encounter_id}")
async def get_encounter(
    encounter_id: str, request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Encounter", encounter_id, request)


@app.get("/fhir/observation/search")
async def search_observations(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Observation", _clean_params(request))


@app.get("/fhir/observation/{obs_id}")
async def get_observation(
    obs_id: str, request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Observation", obs_id, request)


@app.get("/fhir/allergyintolerance/search")
async def search_allergies(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("AllergyIntolerance", _clean_params(request))


@app.get("/fhir/allergyintolerance/{allergy_id}")
async def get_allergy(
    allergy_id: str, request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("AllergyIntolerance", allergy_id, request)


@app.get("/fhir/condition/search")
async def search_conditions(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Condition", _clean_params(request))


@app.get("/fhir/condition/{condition_id}")
async def get_condition(
    condition_id: str, request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Condition", condition_id, request)


@app.get("/fhir/medicationrequest/search")
async def search_medication_requests(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("MedicationRequest", _clean_params(request))


@app.get("/fhir/medicationrequest/{med_id}")
async def get_medication_request(
    med_id: str, request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("MedicationRequest", med_id, request)


# --------------------------------------------------------------------------
# Observation write (vitals capture)
#
# The only write path in the gateway. Everything else is read-only, so this
# endpoint is deliberately narrow: it accepts validated vital signs and emits
# properly coded Observations, rather than proxying arbitrary FHIR bodies. A
# generic passthrough would let any caller write any resource, with no way to
# audit what was actually recorded.
# --------------------------------------------------------------------------


class VitalsWrite(BaseModel):
    """Bounds mirror the CDS service, so a value rejected for scoring can
    never be persisted either."""

    patient_id: str = Field(..., min_length=1, max_length=64)
    respiratory_rate: float | None = Field(default=None, gt=0, le=80)
    spo2: float | None = Field(default=None, ge=1, le=100)
    temperature: float | None = Field(default=None, ge=25, le=45)
    systolic_bp: float | None = Field(default=None, gt=0, le=300)
    pulse: float | None = Field(default=None, gt=0, le=300)
    consciousness: str | None = Field(default=None, max_length=1)
    news2_score: int | None = Field(default=None, ge=0, le=25)
    encounter_id: str | None = Field(default=None, max_length=64)

    @field_validator("consciousness")
    @classmethod
    def _valid_acvpu(cls, v: str | None) -> str | None:
        if v is None:
            return None
        upper = v.strip().upper()
        if upper not in CONSCIOUSNESS_LABELS:
            raise ValueError(
                f"consciousness must be one of {sorted(CONSCIOUSNESS_LABELS)}"
            )
        return upper


@app.post("/fhir/observation", status_code=status.HTTP_201_CREATED, tags=["fhir"])
async def create_observations(
    payload: VitalsWrite,
    request: Request,
    user: User = Depends(clinician_or_admin),
) -> dict[str, Any]:
    """Persist a set of vitals as FHIR Observations.

    Retries are deduplicated with ``Idempotency-Key``: without it, a lost
    response would leave the clinician unsure whether to re-enter the reading,
    and re-entering would double-file it.
    """
    _validate_id(payload.patient_id)
    if payload.encounter_id:
        _validate_id(payload.encounter_id)

    vitals = {
        key: value
        for key, value in (
            ("respiratory_rate", payload.respiratory_rate),
            ("spo2", payload.spo2),
            ("temperature", payload.temperature),
            ("systolic_bp", payload.systolic_bp),
            ("pulse", payload.pulse),
        )
        if value is not None
    }
    if not vitals and payload.consciousness is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one vital sign is required",
        )

    route = "POST /fhir/observation"
    idem_key = extract_idempotency_key(request)
    if idem_key:
        hit = idem_lookup(user.sub, route, idem_key)
        if hit is not None:
            return replay_response(hit[0], hit[1])

    resources = build_vitals_bundle(
        vitals,
        payload.patient_id,
        consciousness=payload.consciousness,
        news2_score=payload.news2_score,
        encounter_id=payload.encounter_id,
        performer=user.sub,
    )

    created: list[dict[str, str]] = []
    for resource in resources:
        try:
            result = await fhir.create("Observation", resource)
        except FHIRError as exc:
            logger.warning(
                "upstream FHIR observation write failed",
                extra={
                    "status_code": exc.status_code,
                    "error_type": type(exc).__name__,
                    # Never log the resource: it carries PHI.
                    "written": len(created),
                },
            )
            # Partial success is reported rather than hidden: the clinician
            # needs to know some readings were filed before deciding whether
            # to re-enter the rest.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "Upstream clinical data service rejected the write "
                    f"after saving {len(created)} of {len(resources)} observations"
                ),
            ) from exc
        created.append(
            {
                "id": str(result.get("id") or ""),
                "code": (result.get("code") or {}).get("text")
                or (resource.get("code") or {}).get("text")
                or "",
            }
        )

    # New observations make any cached search for this patient stale at once.
    # Without this the clinician saves a reading and then cannot see it.
    try:
        await invalidate_cache("Observation", payload.patient_id)
    except Exception:
        logger.warning("observation cache invalidation failed", exc_info=True)

    body = {"ok": True, "created": created, "count": len(created)}
    if idem_key:
        idem_store(user.sub, route, idem_key, 201, body)
    return body


# --------------------------------------------------------------------------
# Admin cache invalidation
# --------------------------------------------------------------------------


@app.delete("/cache/{resource}")
async def admin_invalidate_cache(
    resource: str,
    patient: str | None = Query(default=None),
    user: User = Depends(admin_only),
) -> dict[str, Any]:
    # Normalise case so "patient" and "Patient" both work.
    canonical = next(
        (r for r in KNOWN_RESOURCES if r.lower() == resource.lower()), None
    )
    if canonical is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown resource '{resource}'. "
            f"Expected one of: {', '.join(sorted(KNOWN_RESOURCES))}",
        )
    try:
        deleted = await invalidate_cache(canonical, patient)
    except Exception as exc:
        logger.error("cache invalidation failed", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cache invalidation temporarily unavailable",
        ) from exc
    return {
        "status": "ok",
        "resource": canonical,
        "patient": patient,
        "deleted": deleted,
    }


# --------------------------------------------------------------------------
# Admin audit search — "who viewed MRN-X?"
#
# HIPAA 164.308(a)(1)(ii)(D) requires regular review of information system
# activity, and 164.528 gives patients a right to an accounting of
# disclosures. Both need the audit trail to be *queryable*, not merely
# written. These endpoints are admin-only: the ability to see who looked at
# whom is itself sensitive, and every query here is captured by the same audit
# middleware, so investigating the investigators works too.
# --------------------------------------------------------------------------

# Only these outcomes exist in the index; anything else is a client mistake
# worth reporting rather than an empty result set that looks like "no access".
_AUDIT_OUTCOMES = frozenset({"success", "failure", "denied", "error"})

# Bounds the most expensive query shape (a wide time range with no filters).
MAX_AUDIT_WINDOW_DAYS = 366
DEFAULT_AUDIT_WINDOW_DAYS = 30


def _audit_window(
    since: datetime | None, until: datetime | None
) -> tuple[datetime, datetime]:
    """Resolve and validate the reporting window.

    Defaults to the last 30 days. An unbounded default would make the common
    case a full-table scan as the index grows.
    """
    now = datetime.now(UTC)
    resolved_until = until or now
    resolved_since = since or (resolved_until - timedelta(days=DEFAULT_AUDIT_WINDOW_DAYS))

    # Naive datetimes are ambiguous; assume UTC rather than guessing local.
    if resolved_since.tzinfo is None:
        resolved_since = resolved_since.replace(tzinfo=UTC)
    if resolved_until.tzinfo is None:
        resolved_until = resolved_until.replace(tzinfo=UTC)

    if resolved_since > resolved_until:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'since' must be earlier than 'until'",
        )
    if resolved_until - resolved_since > timedelta(days=MAX_AUDIT_WINDOW_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Time range must not exceed {MAX_AUDIT_WINDOW_DAYS} days",
        )
    return resolved_since, resolved_until


def _audit_unavailable(exc: Exception) -> HTTPException:
    logger.warning(
        "audit search failed",
        extra={"error_type": type(exc).__name__, "error": str(exc)[:200]},
    )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Audit index temporarily unavailable",
    )


@app.get("/audit/search", tags=["audit"])
async def audit_search(
    patient: str | None = Query(
        default=None,
        max_length=64,
        description="Patient/resource identifier, e.g. MRN-123. Hashed before "
        "matching, exactly as the audit writer hashed it.",
    ),
    actor: str | None = Query(default=None, max_length=256),
    outcome: str | None = Query(default=None, max_length=32),
    resource_type: str | None = Query(default=None, max_length=64),
    service: str | None = Query(default=None, max_length=64),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    break_glass: bool | None = Query(
        default=None,
        description="True to review only emergency overrides.",
    ),
    limit: int = Query(default=50, ge=1, le=audit_store.MAX_SEARCH_LIMIT),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(admin_only),
) -> dict[str, Any]:
    """Search the audit trail.

    ``patient`` accepts the *raw* identifier a human knows (an MRN) and hashes
    it with the deployment's audit salt before matching. Callers therefore
    never need to know the pseudonymisation scheme, and no raw identifier is
    written anywhere as a result of searching.
    """
    if outcome and outcome not in _AUDIT_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"outcome must be one of: {', '.join(sorted(_AUDIT_OUTCOMES))}",
        )

    resolved_since, resolved_until = _audit_window(since, until)
    subject_ref = audit_reference(patient.strip()) if patient and patient.strip() else None

    try:
        result = await audit_store.search(
            subject_ref=subject_ref,
            actor=actor.strip() if actor else None,
            outcome=outcome,
            resource_type=resource_type.strip() if resource_type else None,
            service=service.strip() if service else None,
            since=resolved_since,
            until=resolved_until,
            break_glass=break_glass,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        raise _audit_unavailable(exc) from exc

    # Echo the resolved window so a caller relying on defaults knows exactly
    # what was searched, and the pseudonym so results can be cross-referenced
    # against the raw log stream.
    result["since"] = resolved_since.isoformat()
    result["until"] = resolved_until.isoformat()
    result["subject_ref"] = subject_ref
    return result


@app.get("/audit/patient/{patient_id}/accessors", tags=["audit"])
async def audit_patient_accessors(
    patient_id: str,
    limit: int = Query(default=50, ge=1, le=audit_store.MAX_SEARCH_LIMIT),
    user: User = Depends(admin_only),
) -> dict[str, Any]:
    """Who accessed this patient's record, most recent first.

    The summarised form of the same question: one row per clinician with an
    access count and first/last timestamps, which is what a privacy
    investigation starts from.
    """
    _validate_id(patient_id)
    subject_ref = audit_reference(patient_id)
    try:
        accessors = await audit_store.actors_for_subject(subject_ref, limit=limit)
    except Exception as exc:
        raise _audit_unavailable(exc) from exc
    return {
        "patient_ref": subject_ref,
        "accessors": accessors,
        "count": len(accessors),
    }


@app.get("/audit/stats", tags=["audit"])
async def audit_stats(user: User = Depends(admin_only)) -> dict[str, Any]:
    """Audit index health.

    ``dropped`` and ``failed`` are non-zero only when audit records did not
    reach the index. Both should be alerted on: the log stream still has the
    records, but the searchable trail is incomplete until they are backfilled.
    """
    return {
        "enabled": settings.audit_index_enabled,
        "retention_days": settings.audit_retention_days,
        **audit_store.stats(),
    }
