"""MediCore API gateway.

Edge service: enforces JWT auth + RBAC and proxies FHIR reads/searches with a
Postgres-backed response cache.
"""

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from backend.common import audit_store
from backend.common.app import create_service_app
from backend.common.audit_store import search as audit_search_events
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
from backend.common.middleware import audit_reference
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
    try:
        # The audit index is optional: search degrades without it, but the
        # audit stream on stdout - the system of record - is unaffected.
        await audit_store.start_writer()
    except Exception as exc:
        logger.warning(
            "audit index unavailable; audit search disabled",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:200]},
        )

    try:
        yield
    finally:
        # Drain buffered audit rows before the pool closes, or a graceful
        # shutdown silently discards the last few seconds of the trail.
        await audit_store.stop_writer()
        await audit_store.close_pool()
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
    """Readiness: verifies the cache database before accepting traffic."""
    # A non-zero count means audit events exist only on stdout. That is a
    # compliance signal, so it is surfaced rather than left to be discovered
    # during an investigation.
    dropped = audit_store.dropped_events()
    try:
        await cache_ping()
    except Exception:
        logger.warning("readiness check failed", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "cache": "unavailable", "audit_dropped": dropped}
    return {"status": "ok", "cache": "ok", "audit_dropped": dropped}


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


async def _read(resource: str, resource_id: str) -> dict[str, Any]:
    _validate_id(resource_id)
    try:
        return await fhir.read(resource, resource_id)
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
    patient_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Patient", patient_id)


# Kept for backwards compatibility with earlier docs/clients.
@app.get("/fhir/patient/{patient_id}/protected")
async def get_patient_protected(
    patient_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Patient", patient_id)


@app.get("/fhir/encounter/search")
async def search_encounters(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Encounter", _clean_params(request))


@app.get("/fhir/encounter/{encounter_id}")
async def get_encounter(
    encounter_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Encounter", encounter_id)


@app.get("/fhir/observation/search")
async def search_observations(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Observation", _clean_params(request))


@app.get("/fhir/observation/{obs_id}")
async def get_observation(
    obs_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Observation", obs_id)


@app.get("/fhir/allergyintolerance/search")
async def search_allergies(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("AllergyIntolerance", _clean_params(request))


@app.get("/fhir/allergyintolerance/{allergy_id}")
async def get_allergy(
    allergy_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("AllergyIntolerance", allergy_id)


@app.get("/fhir/condition/search")
async def search_conditions(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("Condition", _clean_params(request))


@app.get("/fhir/condition/{condition_id}")
async def get_condition(
    condition_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("Condition", condition_id)


@app.get("/fhir/medicationrequest/search")
async def search_medication_requests(
    request: Request, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _search("MedicationRequest", _clean_params(request))


@app.get("/fhir/medicationrequest/{med_id}")
async def get_medication_request(
    med_id: str, user: User = Depends(clinician_or_admin)
) -> dict[str, Any]:
    return await _read("MedicationRequest", med_id)


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
# Admin: audit search
#
# "Who viewed MRN-X?" is the question HIPAA 164.312(b) exists to answer, and
# until now it could only be answered by grepping a log stream. The caller
# supplies a raw MRN; it is pseudonymised here with the same salt the audit
# middleware used, so the stored index never has to hold a raw identifier for
# the lookup to work.
# --------------------------------------------------------------------------


@app.get("/audit/search", tags=["admin"])
async def audit_search(
    user: User = Depends(admin_only),
    patient: str | None = Query(default=None, max_length=64),
    actor: str | None = Query(default=None, max_length=128),
    resource_type: str | None = Query(default=None, max_length=64),
    outcome: str | None = Query(default=None, max_length=16),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Search the audit trail. Admin only.

    Reading the audit trail is itself an audited event — the middleware records
    this request like any other, so "who went looking through the audit log?"
    stays answerable too.
    """
    if outcome is not None and outcome not in ("success", "failure", "denied", "error"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="outcome must be one of: success, failure, denied, error",
        )

    patient_ref = audit_reference(patient) if patient else None

    try:
        rows, total = await audit_search_events(
            patient_ref=patient_ref,
            actor_sub=actor,
            resource_type=resource_type,
            outcome=outcome,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        logger.error("audit search failed", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit index temporarily unavailable",
        ) from exc

    return {
        "items": rows,
        "count": len(rows),
        "total": total,
        # Echoed so the caller can confirm which patient was searched without
        # the response having to repeat the raw MRN back.
        "patient_ref": patient_ref,
    }


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
