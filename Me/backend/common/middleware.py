"""Structured audit logging.

HIPAA 45 CFR 164.312(b) requires a record of who accessed which patient's
information. That creates a tension with keeping PHI out of the log stream, so
this middleware separates the two concerns:

  * The *audit* record identifies the actor (``sub``) and the resource
    (``resource_type`` + ``resource_ref``) so an accounting of disclosures can
    be produced. The resource reference is a salted hash by default, which is
    stable enough to answer "who viewed this chart?" without writing raw
    identifiers into logs that are shipped off-site.
  * Free-form data that may contain PHI - query parameter *values*, request
    bodies - is never logged. Only parameter names are recorded.

Set ``AUDIT_LOG_RAW_IDENTIFIERS=true`` when the log sink is itself a
HIPAA-compliant, access-controlled store and investigators need raw ids.

Records are additionally handed to an optional *sink* (see
``audit_store.submit``) which indexes them in Postgres so questions like "who
viewed MRN-123?" can be answered directly. The sink is best-effort by
construction: it must never delay or fail a clinical request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("medicore.audit")

# Path shapes that carry a resource identifier.
_FHIR_PATH = re.compile(r"^/fhir/(?P<type>[^/]+)/(?P<id>[^/]+)")
_CACHE_PATH = re.compile(r"^/cache/(?P<type>[^/]+)")
_QUEUE_PATH = re.compile(r"^/queue/(?P<id>[^/]+)")
_BED_PATH = re.compile(r"^/beds/(?P<id>[^/]+)")
_HANDOFF_PATH = re.compile(r"^/handoff/(?P<id>[^/]+)")

# Query parameters whose values identify a patient and must be audited.
_IDENTIFYING_PARAMS = ("patient", "subject", "identifier")


def _load_settings():
    # Imported lazily so this module stays usable in isolation (tests import
    # the middleware without a full settings environment).
    try:
        from .config import settings

        return settings
    except Exception:  # pragma: no cover - defensive
        return None


def pseudonymise(value: str, salt: str) -> str:
    """Stable, non-reversible reference for an identifier.

    The same patient always yields the same token, so access to one chart can
    be correlated across log lines, but the raw identifier is not exposed.
    """
    digest = hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"sha256:{digest[:32]}"


def audit_reference(raw: str | None) -> str | None:
    """The reference this deployment writes into audit records for ``raw``.

    Shared with the audit search endpoint: a query for "MRN-123" must hash the
    identifier exactly as the middleware did, or it silently matches nothing.
    Two implementations of the same rule is how that bug happens, so there is
    only one.
    """
    if not raw:
        return None
    settings = _load_settings()
    if settings is not None and getattr(settings, "audit_log_raw_identifiers", False):
        return raw
    salt = getattr(settings, "audit_log_salt", "") or getattr(
        settings, "jwt_secret", "medicore"
    )
    return pseudonymise(raw, salt)


# Optional queryable sink, installed at startup by a service that owns a
# database (currently the gateway). Left unset everywhere else so the audit
# middleware keeps working standalone.
_sink: Callable[[dict[str, Any]], Any] | None = None


def set_audit_sink(sink: Callable[[dict[str, Any]], Any] | None) -> None:
    """Register (or clear) the queryable audit sink."""
    global _sink
    _sink = sink


# A FHIR id is constrained by the spec to this character set, so anything else
# in the final path segment of a reference is not an id we should record.
_REFERENCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Fields that carry the patient a clinical resource belongs to. ``subject`` is
# the general case; ``patient`` appears on a handful of R4 resource types.
_SUBJECT_FIELDS = ("subject", "patient")


def patient_id_from_resource(resource: Any) -> str | None:
    """Extract the patient id a FHIR resource belongs to, if any.

    Answers "whose record is this?" for a resource that is not itself a
    Patient. Without it, reading ``Observation/obs-1`` records the
    observation's reference and the access never shows up in that patient's
    audit trail — the access happened, but the accounting misses it.

    Handles the reference shapes a real server emits::

        {"subject": {"reference": "Patient/123"}}
        {"subject": {"reference": "https://ehr.example/fhir/Patient/123"}}
        {"patient":  {"reference": "Patient/123"}}

    Returns ``None`` — never raises — when the resource carries no usable
    patient reference. A contained or ``urn:uuid:`` reference is deliberately
    not resolved: it identifies a resource inside the bundle, not a patient
    id the rest of the system would recognise.
    """
    if not isinstance(resource, dict):
        return None

    # A Patient resource is its own subject.
    if resource.get("resourceType") == "Patient":
        raw_id = resource.get("id")
        if isinstance(raw_id, str) and _REFERENCE_ID.match(raw_id):
            return raw_id
        return None

    for field in _SUBJECT_FIELDS:
        node = resource.get(field)
        if not isinstance(node, dict):
            continue
        reference = node.get("reference")
        if not isinstance(reference, str) or not reference:
            continue
        # Only Patient references identify a patient; a subject may legitimately
        # be a Group, Device or Location, and recording those as a patient
        # would put unrelated accesses in someone's disclosure accounting.
        if "Patient/" not in reference:
            continue
        candidate = reference.rsplit("Patient/", 1)[-1].split("/")[0].split("?")[0]
        # "." and ".." are legal FHIR id characters, so a traversal segment
        # like "Patient/../../etc/passwd" slips past the id pattern. This value
        # is written to an audit column and echoed back by search, so reject
        # anything that is only dots.
        if candidate.strip(".") and _REFERENCE_ID.match(candidate):
            return candidate
    return None


def note_audit_patient(request: Request, patient_id: str | None) -> None:
    """Attribute the current request to a patient, for the audit record.

    Called by handlers that only learn the patient after reading the upstream
    resource. The middleware picks this up when it emits, so the access lands
    in that patient's trail rather than being filed under an opaque resource
    id nobody can search for.
    """
    if not patient_id:
        return
    try:
        request.state.audit_patient_id = patient_id
    except Exception:  # pragma: no cover - state is always assignable
        pass


# A search can legitimately disclose a page of patients. The audit record
# names them so each disclosure is attributable, but the list is bounded: an
# unbounded one would let a wide search write an enormous row, and past a
# certain size the useful signal is "this query returned a lot of people",
# which `subject_count` already carries.
MAX_AUDITED_SUBJECTS = 25


def note_audit_patients(request: Request, patient_ids: list[str]) -> None:
    """Attribute a request to every patient it disclosed.

    A search filtered by name or date range returns a set of patients, and
    each of those is a disclosure that belongs in that person's accounting.
    Recording only the query shape means a broad search is invisible in every
    affected patient's trail.
    """
    unique: list[str] = []
    for pid in patient_ids:
        if pid and pid not in unique:
            unique.append(pid)
    if not unique:
        return
    try:
        request.state.audit_patient_ids = unique
        # Keep the primary column populated for a single-subject result, so
        # the common case still answers "who viewed MRN-X?" via patient_ref.
        if len(unique) == 1 and not getattr(request.state, "audit_patient_id", None):
            request.state.audit_patient_id = unique[0]
    except Exception:  # pragma: no cover - state is always assignable
        pass


class AuditLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, service: str | None = None):
        super().__init__(app)
        self.service = service

    def _reference(self, raw: str | None) -> str | None:
        return audit_reference(raw)

    def _describe_target(self, request: Request) -> dict[str, Any]:
        """Identify the clinical record a request touches, for the audit trail."""
        path = request.url.path
        info: dict[str, Any] = {}

        if match := _FHIR_PATH.match(path):
            resource_type = match.group("type")
            identifier = match.group("id")
            info["resource_type"] = resource_type
            if identifier != "search":
                info["resource_ref"] = self._reference(identifier)
        elif match := _CACHE_PATH.match(path):
            info["resource_type"] = match.group("type")
        elif match := _QUEUE_PATH.match(path):
            info["resource_type"] = "TriageQueue"
            info["patient_ref"] = self._reference(match.group("id"))
        elif match := _BED_PATH.match(path):
            info["resource_type"] = "Bed"
            info["bed_id"] = match.group("id")
        elif match := _HANDOFF_PATH.match(path):
            # A handoff note is clinical free text about a named patient, so
            # reading or writing one is an access to that patient's record.
            info["resource_type"] = "HandoffNote"
            info["patient_ref"] = self._reference(match.group("id"))

        # A search filtered by patient is still an access to that patient.
        for name in _IDENTIFYING_PARAMS:
            value = request.query_params.get(name)
            if value:
                info["patient_ref"] = self._reference(value)
                break

        return info

    @staticmethod
    def _redacted_path(path: str) -> str:
        """Path with identifier segments replaced, safe for metrics grouping."""
        if match := _FHIR_PATH.match(path):
            if match.group("id") != "search":
                return f"/fhir/{match.group('type')}/{{id}}"
        if _QUEUE_PATH.match(path):
            return "/queue/{id}"
        if _BED_PATH.match(path):
            return "/beds/{id}"
        if match := _HANDOFF_PATH.match(path):
            # Keep the /history suffix distinguishable from a plain read.
            suffix = "/history" if path.rstrip("/").endswith("/history") else ""
            return f"/handoff/{{id}}{suffix}"
        return path

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id

        forwarded = request.headers.get("x-forwarded-for")
        client_ip = (
            forwarded.split(",")[0].strip()
            if forwarded
            else (request.client.host if request.client else None)
        )

        record: dict[str, Any] = {
            "event": "http_request",
            "request_id": request_id,
            "method": request.method,
            "path": self._redacted_path(request.url.path),
            # Names only - values may be PHI.
            "query_keys": sorted(request.query_params.keys()),
            "client_ip": client_ip,
            "user_agent": request.headers.get("user-agent"),
            **self._describe_target(request),
        }
        if self.service:
            record["service"] = self.service

        try:
            response = await call_next(request)
        except Exception as exc:
            record.update(
                {
                    "status": 500,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                    "outcome": "error",
                    "error_type": type(exc).__name__,
                }
            )
            self._emit(request, record, level=logging.ERROR)
            raise

        record.update(
            {
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "outcome": "success" if response.status_code < 400 else "failure",
            }
        )
        # Authentication runs inside this middleware, so the principal is only
        # attached to request.state by the time the response comes back.
        self._emit(request, record)
        response.headers["x-request-id"] = request_id
        return response

    def _emit(self, request: Request, record: dict[str, Any], level: int = logging.INFO) -> None:
        # Handlers that resolve the patient from the upstream resource annotate
        # request.state; only known once the response exists, hence here rather
        # than in _describe_target. An explicit query filter already identified
        # the patient, so it wins over a resolved one.
        resolved = getattr(request.state, "audit_patient_id", None)
        if resolved and not record.get("patient_ref"):
            record["patient_ref"] = self._reference(str(resolved))

        # A search discloses a page of patients, not just one. Name them all
        # so each person's accounting is complete; the count is recorded
        # separately because the list is truncated for very wide results.
        disclosed = getattr(request.state, "audit_patient_ids", None)
        if disclosed:
            record["subject_count"] = len(disclosed)
            record["subject_refs"] = [
                self._reference(str(pid))
                for pid in disclosed[:MAX_AUDITED_SUBJECTS]
            ]

        # An emergency override is the single most review-worthy event in the
        # trail, so it is carried on the record itself rather than left to be
        # correlated from a separate log line.
        if getattr(request.state, "break_glass", False):
            record["break_glass"] = True
            reason = getattr(request.state, "break_glass_reason", None)
            if reason:
                record["break_glass_reason"] = str(reason)[:500]

        user = getattr(request.state, "user", None)
        if isinstance(user, dict):
            if user.get("sub"):
                record["sub"] = user["sub"]
            if user.get("roles"):
                record["roles"] = user["roles"]
        else:
            principal = getattr(request.state, "principal", None)
            if principal is not None:
                record["sub"] = getattr(principal, "sub", None)
                record["roles"] = getattr(principal, "roles", None)

        # Denied access attempts matter most in an audit trail.
        if record.get("status") in (401, 403):
            record["outcome"] = "denied"
            level = max(level, logging.WARNING)

        logger.log(level, json.dumps(record, default=str))

        # The log stream above is the system of record; the index is derived.
        # It is fed last and defensively, so a broken sink degrades searchability
        # without touching the guarantee that the access was written down.
        sink = _sink
        if sink is not None:
            try:
                sink(record)
            except Exception:  # pragma: no cover - sink must never escape
                logger.debug("audit sink rejected a record", exc_info=True)


def redact_phi_loggers() -> None:
    """Raise third-party HTTP client loggers above INFO.

    httpx (and httpcore/urllib3) log full request URLs at INFO. MediCore URLs
    embed patient identifiers, so leaving them enabled writes PHI into the log
    stream. Applied on import so it holds even when configure_logging has not
    run (for example under pytest).
    """
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


redact_phi_loggers()
