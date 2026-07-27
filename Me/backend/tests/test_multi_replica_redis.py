"""Multi-replica Redis semantics.

``fakeredis.FakeServer`` is a single in-process broker that multiple independent
client objects share — the same topology as N gateway pods talking to one Redis
Deployment. These tests prove the *shared-budget / shared-denylist* property
that fakeredis-per-test alone cannot: a revoke or rate-limit hit on "pod A"
must be visible to "pod B" with zero cross-talk through process memory.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

pytest.importorskip("fakeredis", reason="fakeredis not installed")

import fakeredis  # noqa: E402

from backend.common.hardening import RateLimitMiddleware  # noqa: E402
from backend.common.revocation import (  # noqa: E402
    is_revoked,
    reset_revocation_store,
    revoke,
    revoke_payload,
)
from backend.common.security import create_access_token, verify_access_token  # noqa: E402


@pytest.fixture()
def shared_broker(monkeypatch):
    """One FakeServer, two independent clients — two pods, one Redis."""
    from backend.common import redis_client, revocation

    server = fakeredis.FakeServer()
    pod_a = fakeredis.FakeRedis(server=server, decode_responses=True)
    pod_b = fakeredis.FakeRedis(server=server, decode_responses=True)

    # Default get_redis returns pod_a; tests that need pod_b patch explicitly.
    monkeypatch.setattr(redis_client, "_client", pod_a)
    monkeypatch.setattr(redis_client, "_client_failed", False)
    monkeypatch.setattr(redis_client, "get_redis", lambda: pod_a)
    revocation.reset_revocation_store()
    yield {"server": server, "a": pod_a, "b": pod_b, "redis_client": redis_client}
    revocation.reset_revocation_store()
    pod_a.flushall()


def _rate_limited_app(get_redis_fn, limit: int = 5) -> TestClient:
    """Build an app whose RateLimitMiddleware uses the supplied Redis client."""
    from backend.common import redis_client as rc

    # Point the module-level getter at this pod's client for the duration of
    # the request. Each "pod" is a separate TestClient/app pair.
    original = rc.get_redis

    def _patched():
        return get_redis_fn()

    rc.get_redis = _patched  # type: ignore[assignment]
    try:
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, limit=limit, window_seconds=60)

        @app.get("/x")
        def x():
            return {"ok": True}

        # Keep the patch for as long as the client lives by closing over it.
        client = TestClient(app)
        client._medicore_unpatch = original  # type: ignore[attr-defined]
        client._medicore_rc = rc  # type: ignore[attr-defined]
        return client
    except Exception:
        rc.get_redis = original  # type: ignore[assignment]
        raise


class TestMultiReplicaRateLimit:
    def test_budget_is_shared_across_pods(self, shared_broker, monkeypatch):
        """5 + 5 requests from two pods against limit=5 must yield 429s overall."""
        from backend.common import redis_client

        server = shared_broker["server"]
        # Each pod gets its own client object, same server.
        clients = [
            fakeredis.FakeRedis(server=server, decode_responses=True) for _ in range(2)
        ]

        def make_pod(redis_handle):
            app = FastAPI()
            # Inject the shared-server client via monkeypatch around each call
            # by binding get_redis on the middleware's lookup path.
            app.add_middleware(RateLimitMiddleware, limit=5, window_seconds=60)

            @app.get("/x")
            def x():
                return {"ok": True}

            return app, redis_handle

        apps = [make_pod(c) for c in clients]
        codes: list[int] = []
        for app, handle in apps:
            monkeypatch.setattr(redis_client, "get_redis", lambda h=handle: h)
            with TestClient(app) as tc:
                # 4 from each pod = 8 total against limit 5.
                for _ in range(4):
                    codes.append(tc.get("/x", headers={"X-Forwarded-For": "10.0.0.9"}).status_code)

        assert codes.count(200) == 5, codes
        assert codes.count(429) == 3, codes
        # Single shared counter key.
        keys = list(clients[0].scan_iter("medicore:rl:*"))
        assert len(keys) == 1
        assert int(clients[0].get(keys[0])) >= 5

    def test_in_process_fallback_is_NOT_shared(self, monkeypatch):
        """Control: without Redis each pod has its own budget (the residual)."""
        from backend.common import redis_client

        monkeypatch.setattr(redis_client, "get_redis", lambda: None)
        monkeypatch.setattr(redis_client, "_client", None)
        monkeypatch.setattr(redis_client, "_client_failed", True)

        def pod():
            app = FastAPI()
            app.add_middleware(RateLimitMiddleware, limit=2, window_seconds=60)

            @app.get("/x")
            def x():
                return {"ok": True}

            return TestClient(app)

        a, b = pod(), pod()
        # Each pod independently allows 2.
        assert [a.get("/x").status_code for _ in range(3)] == [200, 200, 429]
        assert [b.get("/x").status_code for _ in range(3)] == [200, 200, 429]


class TestMultiReplicaRevocation:
    def test_revoke_on_pod_a_is_visible_on_pod_b(self, shared_broker, monkeypatch):
        from backend.common import redis_client

        token = create_access_token("alice", roles=["clinician"])
        claims = verify_access_token(token)
        jti = str(claims["jti"])

        # Pod A revokes via its client.
        monkeypatch.setattr(redis_client, "get_redis", lambda: shared_broker["a"])
        assert revoke_payload(claims) is True
        assert shared_broker["a"].exists(f"medicore:revoked:{jti}")

        # Pod B checks via a *different* client object on the same server.
        monkeypatch.setattr(redis_client, "get_redis", lambda: shared_broker["b"])
        # Clear any local in-process denylist so the check must hit Redis.
        reset_revocation_store()
        assert is_revoked(jti) is True
        with pytest.raises(Exception):
            verify_access_token(token)

    def test_local_only_revoke_is_invisible_across_pods(self, monkeypatch):
        """Control: in-process denylist does not cross process boundaries."""
        from backend.common import redis_client, revocation

        monkeypatch.setattr(redis_client, "get_redis", lambda: None)
        revocation.reset_revocation_store()

        token = create_access_token("bob", roles=["clinician"])
        claims = verify_access_token(token)
        # Simulate pod A local revoke.
        revoke(str(claims["jti"]), time.time() + 600)
        assert is_revoked(str(claims["jti"])) is True

        # Simulate pod B: empty local store, no Redis.
        revocation.reset_revocation_store()
        assert is_revoked(str(claims["jti"])) is False
        # Token still verifies on "pod B".
        assert verify_access_token(token)["sub"] == "bob"
