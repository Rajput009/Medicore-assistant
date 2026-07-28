# Design: closing the clinical loop

**Status:** implemented (backend `ab0c820`, UI following). Kept as the
rationale record — the *why* behind the decisions, which the code cannot
state for itself.

## The problem

MediCore records *actions* but not *clinical reasoning or outcomes*. Two
concrete holes, both visible in the current code:

**1. Escalation discards its own evidence.**
`CdsPage` computes a NEWS2 score, maps it to an acuity, and calls `enqueue`.
The repository stores exactly this:

```python
{"patient_id", "acuity", "dept", "status", "created_at", "created_by"}
```

The score that justified the escalation, the band, the red flag, and the
vitals behind it are all thrown away. A charge nurse looking at an ESI-1 in
the queue cannot see *why* it is an ESI-1, and after the fact nobody can
answer "was that escalation appropriate?".

**2. Completion records nothing about what happened.**

```python
{"$set": {"status": "completed", "completed_at": utcnow()}}
```

A patient leaves the queue with no disposition, no clinician named, no
outcome. "Completed" currently means only "removed from the list". It cannot
distinguish *admitted to ICU* from *walked out without being seen* — and those
are the two ends of the safety spectrum a department is measured on.

The result: the queue is a to-do list, not a clinical record. That is the main
thing standing between this scaffold and a product a ward would actually run.

## What this changes

Escalation carries its evidence; completion requires a disposition. Nothing
else about the queue's behaviour changes.

### Data model

Added to a queue document at **enqueue**:

| Field | Type | Notes |
|---|---|---|
| `reason` | text, 10–500 | Why this patient is being escalated. Mandatory when acuity ≤ 2. |
| `news2_score` | int 0–25, optional | The score at escalation |
| `news2_band` | enum, optional | `low` / `low-medium` / `medium` / `high` |
| `red_flag` | bool, optional | Any single parameter scoring 3 |
| `vitals_snapshot` | object, optional | The values behind the score, bounded |
| `escalated_by` | from token | Never from the body |

Added at **completion**:

| Field | Type | Notes |
|---|---|---|
| `disposition` | enum, **required** | see below |
| `disposition_note` | text ≤500, optional | Required for `other` and `left_without_being_seen` |
| `completed_by` | from token | Never from the body |
| `time_to_completion_seconds` | derived | Computed server-side from `created_at` |

### Dispositions

A closed set, because free text cannot be reported on:

- `admitted` — to a ward or unit
- `discharged` — home, with or without follow-up
- `transferred` — to another facility
- `left_without_being_seen` — the safety-critical one; note required
- `deceased`
- `other` — note required

`left_without_being_seen` is deliberately its own value rather than folded
into `other`. It is the outcome an ED is most accountable for, and it must be
countable without parsing prose.

### Why these decisions

**Reason is mandatory only at acuity ≤ 2.** Requiring justification for every
routine ESI-4 would train people to type "." to get past the field, which
degrades the whole dataset. Requiring it where it matters keeps it meaningful.
Minimum length 10, same rule as break-glass, for the same reason: a one-
character reason looks like compliance while providing nothing.

**Evidence is optional, disposition is required.** A clinician may escalate on
clinical judgement with no NEWS2 at all — blocking that would be unsafe, and
they would work around it. But a patient cannot *leave* the queue without
someone saying what happened, because that is the record.

**Attribution comes from the token, never the body.** Same rule as handoff
notes. A record that could claim to be someone else's decision is worse than
no record.

**Snapshot, not reference.** The vitals are copied into the queue entry rather
than linked. A later correction to an Observation must not silently rewrite
the justification for a past decision — the record needs to show what was
known *at the time*.

**Append-only completion.** Completing is a one-way transition. Re-completing
an already-closed entry is a 409, not a silent overwrite, so a disposition
cannot be quietly changed after the fact. Correcting one is a separate,
explicit action (out of scope here, noted below).

### API

```
POST /queue                      # + reason, news2_*, vitals_snapshot
POST /queue/{patient_id}/complete  # + disposition, disposition_note  (now required)
GET  /queue/{patient_id}/history   # the full lifecycle of one entry
GET  /queue/stats?dept=ED&since=…  # counts by disposition, median time to completion
```

`/queue/stats` is what makes this a product rather than a data-entry chore:
the ward gets something back for the typing. Counts by disposition, LWBS rate,
median and 90th-percentile time to completion.

### Breaking change

`POST /queue/{patient_id}/complete` currently takes no body and would now
require `disposition`. Options:

1. **Hard break** — require it immediately. Honest, and the endpoint has no
   external consumers yet.
2. Default to `other` — silently produces a useless dataset. Rejected.
3. Version the endpoint — cost not justified at this stage.

Recommend **(1)**, with the existing tests updated in the same commit so the
break is visible rather than discovered later.

## What it deliberately does not do

- **No clinical advice.** The system records what a clinician decided; it does
  not suggest a disposition. Same boundary as the assistant.
- **No amendment flow.** Correcting a wrong disposition needs an
  append-and-supersede model with its own audit trail. Real, but separate.
- **No FHIR `Encounter` write.** These are MediCore workflow records, not the
  hospital's legal record. Same reasoning as handoff notes: promoting them
  into the EHR is an explicit, separate action.
- **No SLA alerting.** `/queue/stats` reports; it does not page anyone. Alerting
  on a number nobody has validated yet would generate noise, not safety.

## Safety and audit

- Every field above is PHI-adjacent; reads and writes are audited against the
  patient like any other clinical access.
- `reason` and `disposition_note` are clinician free text and are **never
  logged** — only the fact of the write, matching the handoff-note rule.
- Department scope applies unchanged; break-glass applies to completion for
  the same reason it applies to claiming.

## Testing plan

- Disposition enum is closed; an unknown value is a 422, not a stored string.
- `left_without_being_seen` and `other` require a note; a blank one is rejected.
- Reason is mandatory at acuity ≤ 2 and optional above it.
- Re-completing a closed entry is a 409 and does not alter the stored
  disposition.
- `completed_by` and `escalated_by` come from the token even when the body
  tries to set them.
- A vitals snapshot is not mutated by a later Observation change.
- `/queue/stats` counts match the underlying documents, including the LWBS
  rate, and an empty window returns zeros rather than an error.
- Free-text fields never appear in the audit log stream.
- Live-stack: escalate with evidence → claim → complete with disposition →
  the whole lifecycle is retrievable and audited.

## Estimated shape

~400 lines of production code (model, repository, three endpoints, UI form
changes), ~600 lines of tests. Two commits: backend + tests, then UI.

## Open questions for you

1. **Dispositions** — is that list right for your setting, or is it
   ED-specific? A ward-based deployment might need `escalated_to_outreach` or
   `treatment_completed`.
2. **Reason threshold** — mandatory at acuity ≤ 2 is my guess. Should it be
   all escalations, or only ESI-1?
3. **Is `/queue/stats` the right sweetener**, or would you rather the first
   version stayed purely about capture?
