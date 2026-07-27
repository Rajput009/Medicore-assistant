"""Repository tests against a MongoDB-semantics engine.

These exercise the real ``PatientFlowRepository`` (real queries, real
find_one_and_update, real unique-index behaviour) rather than a hand-written
fake, so mistakes in the query documents themselves are caught.

``mongomock_motor`` implements the wire semantics in-process. Where it diverges
from a live server the test says so explicitly.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mongomock_motor", reason="mongomock_motor not installed")

from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from backend.services.patient_flow.repository import (  # noqa: E402
    ConflictError,
    NotFoundError,
    PatientFlowRepository,
)

BEDS = [
    {"bed_id": "A-001", "ward": "A"},
    {"bed_id": "A-002", "ward": "A"},
    {"bed_id": "ICU-001", "ward": "ICU"},
]


@pytest.fixture()
def event_loop():
    """Own the loop explicitly; asyncio.get_event_loop() is deprecated and
    raises once another test has closed the implicit loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture()
def repo(event_loop):
    client = AsyncMongoMockClient()
    repository = PatientFlowRepository(client["medicore_test"])
    repository._loop = event_loop
    event_loop.run_until_complete(repository.seed_beds(BEDS))
    return repository


def run(coro):
    """Drive a coroutine on the fixture-owned loop."""
    return asyncio.get_event_loop_policy().get_event_loop().run_until_complete(coro)


class TestSeeding:
    def test_creates_all_beds(self, repo):
        assert len(run(repo.list_beds())) == 3

    def test_is_idempotent(self, repo):
        """Two replicas starting at once must not duplicate beds."""
        assert run(repo.seed_beds(BEDS)) == 0
        assert len(run(repo.list_beds())) == 3

    def test_does_not_reset_occupancy(self, repo):
        run(repo.set_bed_occupancy("A-001", True, "MRN-1"))
        run(repo.seed_beds(BEDS))
        assert run(repo.get_bed("A-001"))["occupied"] is True

    def test_adds_new_beds_without_touching_existing(self, repo):
        run(repo.set_bed_occupancy("A-001", True, "MRN-1"))
        created = run(repo.seed_beds([*BEDS, {"bed_id": "B-001", "ward": "B"}]))
        assert created == 1
        assert run(repo.get_bed("A-001"))["patient_id"] == "MRN-1"


class TestBeds:
    def test_filter_by_ward(self, repo):
        assert [b["bed_id"] for b in run(repo.list_beds(ward="ICU"))] == ["ICU-001"]

    def test_filter_by_occupancy(self, repo):
        run(repo.set_bed_occupancy("A-001", True, "MRN-1"))
        free = run(repo.list_beds(occupied=False))
        assert "A-001" not in [b["bed_id"] for b in free]
        assert len(free) == 2

    def test_assign_then_release(self, repo):
        assigned = run(repo.set_bed_occupancy("A-001", True, "MRN-1"))
        assert assigned["patient_id"] == "MRN-1"
        released = run(repo.set_bed_occupancy("A-001", False, None))
        assert released["occupied"] is False
        assert released["patient_id"] is None

    def test_release_always_clears_the_patient(self, repo):
        """Even if a caller passes a patient id while releasing."""
        run(repo.set_bed_occupancy("A-001", True, "MRN-1"))
        assert run(repo.set_bed_occupancy("A-001", False, "MRN-1"))["patient_id"] is None

    def test_unknown_bed_raises(self, repo):
        with pytest.raises(NotFoundError):
            run(repo.get_bed("NOPE"))
        with pytest.raises(NotFoundError):
            run(repo.set_bed_occupancy("NOPE", True, "MRN-1"))

    def test_conditional_update_succeeds_when_expectation_holds(self, repo):
        bed = run(repo.set_bed_occupancy("A-001", True, "MRN-1", expected_occupied=False))
        assert bed["occupied"] is True

    def test_conditional_update_fails_when_expectation_breaks(self, repo):
        run(repo.set_bed_occupancy("A-001", True, "MRN-1"))
        with pytest.raises(ConflictError):
            run(repo.set_bed_occupancy("A-001", True, "MRN-2", expected_occupied=False))
        # The original assignment survives.
        assert run(repo.get_bed("A-001"))["patient_id"] == "MRN-1"

    def test_conflict_is_distinguished_from_missing(self, repo):
        """A conditional update on a nonexistent bed is 'not found', not
        'conflict' — the caller needs to tell these apart."""
        with pytest.raises(NotFoundError):
            run(repo.set_bed_occupancy("GHOST", True, "MRN-1", expected_occupied=False))

    def test_concurrent_assignment_has_exactly_one_winner(self, repo):
        """Two clinicians racing for the last bed: one must lose."""

        async def scenario():
            results = await asyncio.gather(
                repo.set_bed_occupancy("A-002", True, "MRN-1", expected_occupied=False),
                repo.set_bed_occupancy("A-002", True, "MRN-2", expected_occupied=False),
                return_exceptions=True,
            )
            return results

        results = run(scenario())
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, ConflictError)]
        assert len(winners) == 1
        assert len(losers) == 1


class TestQueue:
    def test_enqueue_and_list(self, repo):
        run(repo.enqueue("MRN-1", 3, "ED", "nurse.1"))
        items = run(repo.list_queue(limit=10))
        assert len(items) == 1
        assert items[0]["status"] == "waiting"
        assert items[0]["created_by"] == "nurse.1"

    def test_ordering_by_acuity_then_arrival(self, repo):
        run(repo.enqueue("low", 5, "ED", "n"))
        run(repo.enqueue("urgent", 1, "ED", "n"))
        run(repo.enqueue("mid", 3, "ED", "n"))
        assert [i["patient_id"] for i in run(repo.list_queue(limit=10))] == [
            "urgent",
            "mid",
            "low",
        ]

    def test_fifo_within_the_same_acuity(self, repo):
        run(repo.enqueue("first", 3, "ED", "n"))
        run(repo.enqueue("second", 3, "ED", "n"))
        assert [i["patient_id"] for i in run(repo.list_queue(limit=10))] == [
            "first",
            "second",
        ]

    def test_limit_and_total_differ(self, repo):
        for i in range(5):
            run(repo.enqueue(f"MRN-{i}", 3, "ED", "n"))
        assert len(run(repo.list_queue(limit=2))) == 2
        assert run(repo.count_queue()) == 5

    def test_department_filter(self, repo):
        run(repo.enqueue("ed", 3, "ED", "n"))
        run(repo.enqueue("icu", 3, "ICU", "n"))
        assert [i["patient_id"] for i in run(repo.list_queue(10, dept="ICU"))] == ["icu"]

    def test_claim_returns_most_urgent_and_removes_from_waiting(self, repo):
        run(repo.enqueue("low", 5, "ED", "n"))
        run(repo.enqueue("urgent", 1, "ED", "n"))
        claimed = run(repo.claim_next("ED", "dr.a"))
        assert claimed["patient_id"] == "urgent"
        assert claimed["claimed_by"] == "dr.a"
        assert run(repo.count_queue()) == 1

    def test_claim_is_scoped_to_department(self, repo):
        run(repo.enqueue("icu", 1, "ICU", "n"))
        assert run(repo.claim_next("ED", "dr.a")) is None

    def test_claim_on_empty_queue_returns_none(self, repo):
        assert run(repo.claim_next("ED", "dr.a")) is None

    def test_concurrent_claims_never_return_the_same_patient(self, repo):
        """find_one_and_update is atomic, so only one caller can win."""
        run(repo.enqueue("only-one", 1, "ED", "n"))

        async def scenario():
            return await asyncio.gather(
                repo.claim_next("ED", "dr.a"), repo.claim_next("ED", "dr.b")
            )

        results = run(scenario())
        claimed = [r for r in results if r is not None]
        assert len(claimed) == 1

    def test_complete_marks_the_entry(self, repo):
        run(repo.enqueue("MRN-1", 3, "ED", "n"))
        assert run(repo.complete("MRN-1", disposition="discharged", completed_by="dr.test"))["status"] == "completed"
        assert run(repo.count_queue()) == 0

    def test_complete_unknown_patient_raises(self, repo):
        with pytest.raises(NotFoundError):
            run(repo.complete("ghost", disposition="discharged", completed_by="dr.test"))

    def test_completing_twice_is_a_conflict_not_a_not_found(self, repo):
        """Re-completing must not silently rewrite the recorded outcome.

        ConflictError rather than NotFoundError: the patient plainly exists,
        and reporting "not found" for a closed entry sent the clinician
        looking for a vanished patient. A conflict says what is actually
        true — this is already closed.
        """
        run(repo.enqueue("MRN-1", 3, "ED", "n"))
        run(repo.complete("MRN-1", disposition="admitted", completed_by="dr.a"))
        with pytest.raises(ConflictError):
            run(repo.complete("MRN-1", disposition="discharged", completed_by="dr.b"))
        # The original disposition survives the attempt.
        history = run(repo.queue_history("MRN-1"))
        assert history[0]["disposition"] == "admitted"
        assert history[0]["completed_by"] == "dr.a"

    def test_requeue_after_completion(self, repo):
        run(repo.enqueue("MRN-1", 3, "ED", "n"))
        run(repo.complete("MRN-1", disposition="discharged", completed_by="dr.test"))
        run(repo.enqueue("MRN-1", 1, "ED", "n"))
        assert run(repo.count_queue()) == 1

    def test_claimed_patient_can_be_completed(self, repo):
        run(repo.enqueue("MRN-1", 1, "ED", "n"))
        run(repo.claim_next("ED", "dr.a"))
        assert run(repo.complete("MRN-1", disposition="discharged", completed_by="dr.test"))["status"] == "completed"


class TestMultiReplicaConsistency:
    """Regression: bed state was previously held in process memory, so each
    replica invented its own bed ids and lost every write."""

    def test_replicas_share_identical_bed_ids(self, event_loop):
        from backend.services.patient_flow.main import build_bed_documents

        shared = AsyncMongoMockClient()["medicore"]
        pod_a = PatientFlowRepository(shared)
        pod_b = PatientFlowRepository(shared)

        async def scenario():
            for pod in (pod_a, pod_b):
                await pod.seed_beds(build_bed_documents())
            return (
                [b["bed_id"] for b in await pod_a.list_beds()],
                [b["bed_id"] for b in await pod_b.list_beds()],
            )

        ids_a, ids_b = event_loop.run_until_complete(scenario())
        assert ids_a == ids_b
        assert len(ids_a) == len(build_bed_documents())

    def test_a_write_on_one_replica_is_visible_on_another(self, event_loop):
        from backend.services.patient_flow.main import build_bed_documents

        shared = AsyncMongoMockClient()["medicore"]
        pod_a = PatientFlowRepository(shared)
        pod_b = PatientFlowRepository(shared)

        async def scenario():
            await pod_a.seed_beds(build_bed_documents())
            await pod_b.seed_beds(build_bed_documents())
            await pod_a.set_bed_occupancy("A-001", True, "MRN-42")
            return await pod_b.get_bed("A-001")

        bed = event_loop.run_until_complete(scenario())
        assert bed["occupied"] is True
        assert bed["patient_id"] == "MRN-42"

    def test_concurrent_seeding_does_not_duplicate(self, event_loop):
        """Replicas starting simultaneously must not create duplicate beds."""
        from backend.services.patient_flow.main import build_bed_documents

        shared = AsyncMongoMockClient()["medicore"]
        pods = [PatientFlowRepository(shared) for _ in range(3)]

        async def scenario():
            docs = build_bed_documents()
            await asyncio.gather(*(p.seed_beds(docs) for p in pods))
            return await pods[0].list_beds()

        beds = event_loop.run_until_complete(scenario())
        ids = [b["bed_id"] for b in beds]
        assert len(ids) == len(set(ids)) == len(build_bed_documents())
