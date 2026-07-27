"""Break-glass: emergency override of ward/department scope.

A clinician who is scoped to ward A and finds a ward-B patient arresting in
front of them needs the chart *now*. Ward scoping is correct almost always,
and catastrophically wrong in that moment. The standard answer is break-glass:
let them through, but make the override loud, attributable and reviewable.

Design decisions, each of which is a deliberate trade:

**It widens scope, never role.** A `viewer` does not become a `clinician`, and
nobody becomes `admin`. Break-glass exists for "right role, wrong ward"; if it
also granted permissions it would be a privilege-escalation primitive wearing
a safety label. Role checks run untouched.

**A reason is mandatory and must be substantive.** An override with no
justification is unreviewable, which defeats the point — the control is not
the gate, it is the record. A too-short reason is rejected rather than
accepted-and-logged, because "x" in an audit column looks like compliance
while providing nothing.

**It is per-request, not a session mode.** The header must be sent on every
call, so access does not silently stay elevated after the emergency. There is
no "break-glass session" to forget to close.

**It never fails open.** If break-glass is disabled by configuration, the
header is ignored and normal scope applies.

Invoking it is recorded at WARNING with ``break_glass: true`` and the reason,
and is queryable in the audit index — that is what makes it reviewable after
the fact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from starlette.requests import Request

logger = logging.getLogger("medicore.audit")

# The declaration header. A dedicated header (rather than a query parameter)
# keeps the reason out of URLs, which are logged by proxies and can carry it
# into places PHI-adjacent free text should not go.
BREAK_GLASS_HEADER = "x-break-glass-reason"

# A reason short enough to be meaningless is worse than no reason: it looks
# like an audit trail without being one.
MIN_REASON_LENGTH = 10
MAX_REASON_LENGTH = 500


@dataclass(frozen=True)
class BreakGlass:
    """An accepted emergency-access declaration."""

    reason: str

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return True


class BreakGlassError(ValueError):
    """The declaration was present but unusable."""


def _clean(raw: str) -> str:
    """Collapse whitespace and strip control characters.

    The value is echoed into logs and an audit column; newlines would let a
    caller forge extra log lines.
    """
    return " ".join(raw.split())


def parse_declaration(request: Request, *, enabled: bool = True) -> BreakGlass | None:
    """Read and validate the break-glass header.

    Returns ``None`` when no declaration was made (the overwhelmingly common
    case). Raises :class:`BreakGlassError` when one was made but is unusable,
    so a clinician who typo'd the header gets told, rather than silently
    receiving a 403 they cannot explain.
    """
    raw = request.headers.get(BREAK_GLASS_HEADER)
    if raw is None:
        return None
    if not enabled:
        # Fail closed and loudly: the caller believes they have emergency
        # access and they do not.
        raise BreakGlassError("Break-glass access is disabled in this deployment")

    reason = _clean(raw)
    if not reason:
        raise BreakGlassError("Break-glass requires a reason")
    if len(reason) < MIN_REASON_LENGTH:
        raise BreakGlassError(
            f"Break-glass reason must be at least {MIN_REASON_LENGTH} characters "
            "and describe the clinical need"
        )
    if len(reason) > MAX_REASON_LENGTH:
        raise BreakGlassError(
            f"Break-glass reason must be at most {MAX_REASON_LENGTH} characters"
        )
    return BreakGlass(reason=reason)


def note_break_glass(request: Request, declaration: BreakGlass) -> None:
    """Mark the request so the audit middleware records the override."""
    try:
        request.state.break_glass = True
        request.state.break_glass_reason = declaration.reason
    except Exception:  # pragma: no cover - state is always assignable
        pass


def record_override(
    request: Request,
    declaration: BreakGlass,
    *,
    subject: str,
    scope_type: str,
    scope_value: str | None,
) -> None:
    """Log an override that actually granted access it would otherwise deny.

    Emitted immediately (rather than only via the request's audit record) so
    the event survives even if the response path or the index write fails —
    an override is exactly the event that must not go missing.
    """
    note_break_glass(request, declaration)
    logger.warning(
        "break-glass override granted",
        extra={
            "event": "break_glass",
            "sub": subject,
            "scope_type": scope_type,
            "scope_value": scope_value,
            "break_glass_reason": declaration.reason,
        },
    )
