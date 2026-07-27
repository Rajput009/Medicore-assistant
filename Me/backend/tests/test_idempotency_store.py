"""Replay fidelity of the idempotency store.

A replayed response is supposed to be *the response the client missed*. Two
bugs broke that on the in-process path (the default when Redis is off):

1. ``lookup`` returned the stored ``{"status", "body"}`` envelope instead of
   the body, so a retry received a completely different JSON shape.
2. ``store`` serialised datetimes with ``str()`` ("2026-07-27 20:11:10+00:00")
   while FastAPI emits ISO-8601 ("2026-07-27T20:11:10+00:00"), so a retry
   received timestamps a strict client cannot parse.

Both hid behind assertions that only checked the status code.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.common.idempotency import (
    extract_idempotency_key,
    lookup,
    reset_idempotency_store,
    store,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_idempotency_store()
    yield
    reset_idempotency_store()


class TestReplayFidelity:
    def test_lookup_returns_the_body_not_the_envelope(self):
        body = {"ok": True, "item": {"patient_id": "MRN-1"}}
        store("u1", "POST /queue", "k1", 201, body)

        hit = lookup("u1", "POST /queue", "k1")
        assert hit is not None
        status_code, replayed = hit
        assert status_code == 201
        assert replayed == body
        # The envelope keys must not leak into the caller's body.
        assert "status" not in replayed
        assert "body" not in replayed

    def test_datetimes_replay_in_iso_8601(self):
        """Must match FastAPI's encoding, not str()."""
        moment = datetime(2026, 7, 27, 20, 11, 10, tzinfo=UTC)
        store("u1", "POST /queue", "k2", 200, {"created_at": moment})

        _, replayed = lookup("u1", "POST /queue", "k2")
        assert "T" in replayed["created_at"]
        assert " " not in replayed["created_at"]

    def test_nested_datetimes_are_encoded_too(self):
        moment = datetime(2026, 7, 27, 20, 11, 10, tzinfo=UTC)
        store("u1", "POST /x", "k3", 200, {"item": {"completed_at": moment}})

        _, replayed = lookup("u1", "POST /x", "k3")
        assert "T" in replayed["item"]["completed_at"]


class TestKeyIsolation:
    def test_a_miss_returns_none(self):
        assert lookup("u1", "POST /queue", "never-seen") is None

    def test_keys_are_scoped_per_principal(self):
        """One clinician's retry must never replay another's response."""
        store("u1", "POST /queue", "same", 201, {"who": "u1"})
        assert lookup("u2", "POST /queue", "same") is None

    def test_keys_are_scoped_per_route(self):
        store("u1", "POST /queue", "same", 201, {"route": "enqueue"})
        assert lookup("u1", "POST /queue/MRN-1/complete", "same") is None


class TestKeyValidation:
    def _request(self, value: str | None):
        class _Req:
            headers = {} if value is None else {"idempotency-key": value}

        return _Req()

    def test_absent_key_is_none(self):
        assert extract_idempotency_key(self._request(None)) is None

    def test_blank_key_is_none(self):
        assert extract_idempotency_key(self._request("   ")) is None

    def test_normal_uuid_is_accepted(self):
        key = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        assert extract_idempotency_key(self._request(key)) == key

    def test_overlong_key_is_rejected(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            extract_idempotency_key(self._request("x" * 200))
        assert exc.value.status_code == 400

    def test_control_characters_are_rejected(self):
        """Header injection / log-forging guard."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            extract_idempotency_key(self._request("abc\ndef"))
        assert exc.value.status_code == 400
