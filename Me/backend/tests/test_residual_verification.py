"""Evidence suite for residual production risks.

These tests do **not** claim the residual gaps are closed. They pin down exactly
what is proven, what is mock-shaped, and what still needs a real cluster /
broker / mongod. Every assertion below is something we can re-run in CI.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from jose import JWTError
from starlette.testclient import TestClient

from backend.common.config import Settings
from backend.common.security import create_access_token, verify_access_token

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "k8s" / "base"


# ---------------------------------------------------------------------------
# 1. Redis-backed rate limit + revocation (via fakeredis drop-in)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_redis(monkeypatch):
    """Wire a process-local Redis emulator into the shared client.

    This is not a real Redis server, but it exercises the *same code paths*
    (INCR/EXPIRE, SETEX, EXISTS) that production hits when REDIS_ENABLED=true.
    """
    fakeredis = pytest.importorskip("fakeredis", reason="fakeredis not installed")
    from backend.common import redis_client, revocation

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_client", client)
    monkeypatch.setattr(redis_client, "_client_failed", False)
    monkeypatch.setattr(
        redis_client, "get_redis", lambda: client
    )
    revocation.reset_revocation_store()
    yield client
    revocation.reset_revocation_store()
    client.flushall()


class TestRedisBackedControls:
    def test_revocation_uses_redis_key_with_ttl(self, fake_redis):
        from backend.common.revocation import is_revoked, revoke

        exp = time.time() + 120
        revoke("jti-redis-1", exp)
        assert fake_redis.exists("medicore:revoked:jti-redis-1")
        ttl = fake_redis.ttl("medicore:revoked:jti-redis-1")
        assert 1 <= ttl <= 120
        assert is_revoked("jti-redis-1") is True
        assert is_revoked("jti-other") is False

    def test_verify_rejects_token_revoked_via_redis(self, fake_redis):
        from backend.common.revocation import revoke_payload

        token = create_access_token("alice", roles=["clinician"])
        claims = verify_access_token(token)
        assert claims.get("jti")
        assert revoke_payload(claims) is True
        # Key landed in Redis, not just the in-process map.
        assert fake_redis.exists(f"medicore:revoked:{claims['jti']}")
        # Specific: a bare Exception would still pass if verification broke
        # for some reason unrelated to the denylist.
        with pytest.raises(JWTError, match="revoked"):
            verify_access_token(token)

    def test_rate_limit_uses_redis_incr(self, fake_redis):
        from backend.common.hardening import RateLimitMiddleware

        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, limit=3, window_seconds=60)

        @app.get("/x")
        def x():
            return {"ok": True}

        client = TestClient(app)
        codes = [client.get("/x").status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        # Shared key prefix proves we hit the Redis branch.
        keys = list(fake_redis.scan_iter("medicore:rl:*"))
        assert keys, "expected a medicore:rl:* key after rate-limited traffic"
        assert int(fake_redis.get(keys[0])) >= 3

    def test_rate_limit_falls_back_when_redis_errors(self, monkeypatch):
        """A dead Redis must not 500 the API — in-process limiter takes over."""
        from backend.common import redis_client
        from backend.common.hardening import RateLimitMiddleware

        class Boom:
            def pipeline(self):
                raise ConnectionError("redis down")

        monkeypatch.setattr(redis_client, "_client", Boom())
        monkeypatch.setattr(redis_client, "_client_failed", False)
        monkeypatch.setattr(redis_client, "get_redis", lambda: Boom())

        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, limit=2, window_seconds=60)

        @app.get("/x")
        def x():
            return {"ok": True}

        client = TestClient(app)
        assert [client.get("/x").status_code for _ in range(3)] == [200, 200, 429]


# ---------------------------------------------------------------------------
# 2. MongoDB: what mongomock-motor actually enforces
# ---------------------------------------------------------------------------


class TestMongoMockIndexSemantics:
    """Document the safety properties the mock does and does not give us.

    Real mongod is unreachable from this environment (TLS to fastdl.mongodb.org
    fails). These tests lock the mock behaviour so a future upgrade that breaks
    partial-index emulation is loud.
    """

    @pytest.fixture()
    def queue(self):
        pytest.importorskip("mongomock_motor", reason="mongomock_motor not installed")
        import asyncio

        from mongomock_motor import AsyncMongoMockClient
        from pymongo import ASCENDING

        loop = asyncio.new_event_loop()
        client = AsyncMongoMockClient()
        db = client["medicore_test"]

        async def setup():
            await db.queue.create_index(
                [("patient_id", ASCENDING)],
                unique=True,
                partialFilterExpression={"status": "waiting"},
            )
            return db.queue

        q = loop.run_until_complete(setup())
        yield loop, q
        loop.close()

    def test_two_waiting_slots_for_same_patient_are_rejected(self, queue):
        from pymongo.errors import DuplicateKeyError

        loop, q = queue

        async def scenario():
            await q.insert_one({"patient_id": "P1", "status": "waiting", "acuity": 1})
            with pytest.raises(DuplicateKeyError):
                await q.insert_one(
                    {"patient_id": "P1", "status": "waiting", "acuity": 2}
                )

        loop.run_until_complete(scenario())

    def test_completed_row_does_not_block_a_new_waiting_row(self, queue):
        """Partial filter: only status=waiting participates in the unique key."""

        loop, q = queue

        async def scenario():
            await q.insert_one({"patient_id": "P1", "status": "waiting"})
            # Mark the waiting row completed (real flow does find_one_and_update).
            await q.update_one(
                {"patient_id": "P1", "status": "waiting"},
                {"$set": {"status": "completed"}},
            )
            # Re-queue must succeed under a real partial unique index.
            await q.insert_one({"patient_id": "P1", "status": "waiting"})
            n = await q.count_documents({"patient_id": "P1", "status": "waiting"})
            assert n == 1

        loop.run_until_complete(scenario())

    def test_repository_enqueue_conflict_matches_index(self, queue):
        """End-to-end through PatientFlowRepository against the mock."""
        import asyncio

        from mongomock_motor import AsyncMongoMockClient

        from backend.services.patient_flow.repository import (
            ConflictError,
            PatientFlowRepository,
        )

        loop = asyncio.new_event_loop()
        client = AsyncMongoMockClient()
        repo = PatientFlowRepository(client["medicore"])

        async def scenario():
            await repo.ensure_indexes()
            await repo.enqueue("MRN-1", 2, "ED", "nurse")
            with pytest.raises(ConflictError):
                await repo.enqueue("MRN-1", 1, "ED", "nurse")
            await repo.complete("MRN-1")
            # After completion the patient may wait again.
            await repo.enqueue("MRN-1", 3, "ED", "nurse")
            assert await repo.count_queue() == 1

        loop.run_until_complete(scenario())
        loop.close()


# ---------------------------------------------------------------------------
# 3. Access-token lifetime boundary (refresh-token gap is intentional)
# ---------------------------------------------------------------------------


class TestTokenLifetimeBoundary:
    def test_default_ttl_is_fifteen_minutes(self):
        assert Settings().access_token_ttl_minutes == 15

    def test_production_rejects_ttl_above_one_hour(self):
        with pytest.raises(ValueError, match="ACCESS_TOKEN_TTL"):
            Settings(
                env="production",
                jwt_secret="s" * 40,
                session_secret="t" * 40,
                postgres_password="p" * 20,
                fhir_client_secret="real-secret",
                trusted_hosts="api.example",
                oidc_issuer="https://idp.example",
                oidc_client_id="c",
                oidc_client_secret="s" * 20,
                access_token_ttl_minutes=120,
            )

    def test_no_refresh_token_is_issued_by_create_access_token(self):
        """Pin the current design: only access tokens exist.

        If a future change adds refresh tokens, this test forces an explicit
        decision about token_use discrimination and rotation.
        """
        token = create_access_token("alice", roles=["clinician"])
        claims = verify_access_token(token)
        assert claims["token_use"] == "access"
        assert "refresh" not in claims
        # minting does not return a refresh companion
        assert set(claims) >= {"sub", "roles", "exp", "iat", "jti", "token_use"}


# ---------------------------------------------------------------------------
# 4. NetworkPolicy structure (mesh mTLS is NOT present — assert the L3/L4 net)
# ---------------------------------------------------------------------------


class TestNetworkPolicyStructure:
    @pytest.fixture(scope="class")
    def policies(self):
        path = K8S / "networkpolicy.yaml"
        assert path.exists(), "networkpolicy.yaml missing"
        docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
        by_name = {d["metadata"]["name"]: d for d in docs}
        return by_name

    def test_default_deny_ingress_and_egress_exist(self, policies):
        assert "default-deny-ingress" in policies
        assert "default-deny-egress" in policies
        assert policies["default-deny-ingress"]["spec"]["policyTypes"] == ["Ingress"]
        assert policies["default-deny-egress"]["spec"]["policyTypes"] == ["Egress"]

    def test_gateway_ingress_only_from_ingress_controller(self, policies):
        ingress = policies["gateway-ingress"]["spec"]["ingress"]
        assert len(ingress) == 1
        froms = ingress[0]["from"]
        assert any(
            f.get("namespaceSelector", {}).get("matchLabels", {}).get("name")
            == "ingress-nginx"
            for f in froms
        )

    def test_internal_services_do_not_accept_arbitrary_pods(self, policies):
        ingress = policies["internal-services-ingress"]["spec"]["ingress"]
        allowed = []
        for rule in ingress:
            for f in rule.get("from", []):
                if "podSelector" in f:
                    allowed.append(f["podSelector"])
                if "namespaceSelector" in f:
                    allowed.append(f["namespaceSelector"])
        # Must not be an empty from: (which would mean "all pods").
        assert allowed, "internal ingress has no from: selectors"
        # Must not select the entire namespace with an empty podSelector.
        for sel in allowed:
            if "matchLabels" in sel:
                assert sel["matchLabels"], "empty matchLabels would allow everyone"

    def test_each_workload_has_an_egress_allow_list(self, policies):
        for name in ("gateway-egress", "auth-egress", "patient-flow-egress", "cds-egress"):
            assert name in policies, f"missing {name}"
            egress = policies[name]["spec"].get("egress") or []
            assert egress, f"{name} has empty egress (would deny everything including DNS)"
            # DNS must be reachable or the pod cannot resolve anything.
            ports = [
                (p.get("port"), p.get("protocol"))
                for rule in egress
                for p in rule.get("ports") or []
            ]
            assert (53, "UDP") in ports or (53, "TCP") in ports, f"{name} missing DNS"

    def test_mesh_mtls_manifests_are_present_and_strict(self):
        """Istio PeerAuthentication ships in STRICT mode with SA identities.

        Inert without an Istio control plane; the residual is "mesh not
        running", not "policy not designed".
        """
        path = K8S / "istio-mtls.yaml"
        assert path.exists(), "istio-mtls.yaml missing"
        docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
        kinds = {d.get("kind") for d in docs}
        assert "PeerAuthentication" in kinds
        assert "AuthorizationPolicy" in kinds
        assert "DestinationRule" in kinds
        assert "ServiceAccount" in kinds
        peer = next(d for d in docs if d.get("kind") == "PeerAuthentication")
        assert peer["spec"]["mtls"]["mode"] == "STRICT"
        # Deployments must reference the SAs so SPIFFE principals resolve.
        for deploy_name, sa in (
            ("gateway.yaml", "gateway"),
            ("auth.yaml", "auth"),
            ("patient-flow.yaml", "patient-flow"),
            ("cds.yaml", "cds"),
        ):
            text = (K8S / deploy_name).read_text()
            assert f"serviceAccountName: {sa}" in text, deploy_name

    def test_fqdn_egress_config_forbids_wildcard_star(self):
        """Cilium FQDN allow-list must never re-open 0.0.0.0/0 via '*'."""
        path = K8S / "cilium-egress.yaml"
        assert path.exists(), "cilium-egress.yaml missing"
        docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
        cm = next(
            d
            for d in docs
            if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "medicore-egress-fqdns"
        )
        for key in ("fhir_hosts", "idp_hosts", "otlp_hosts"):
            value = cm["data"][key].strip()
            assert value, f"{key} must not be empty"
            assert value != "*", f"{key} must not be a bare wildcard"
            for part in value.split(","):
                part = part.strip()
                assert part and part != "*", f"{key} contains bare *"
        # Cilium policies must use toFQDNs somewhere.
        cilium = [d for d in docs if d.get("kind") == "CiliumNetworkPolicy"]
        assert cilium, "expected CiliumNetworkPolicy objects"
        blob = yaml.dump(cilium)
        assert "toFQDNs" in blob
        assert "0.0.0.0/0" not in blob, "Cilium policy must not re-open the world"


# ---------------------------------------------------------------------------
# 5. Ingress path routing matches the SPA proxy contract
# ---------------------------------------------------------------------------


class TestIngressPathRouting:
    def test_api_auth_flow_cds_prefixes_are_routed(self):
        docs = list(yaml.safe_load_all((K8S / "ingress.yaml").read_text()))
        ing = docs[0]
        paths = []
        for rule in ing["spec"]["rules"]:
            for p in rule.get("http", {}).get("paths", []):
                paths.append(
                    (
                        p["path"],
                        p["backend"]["service"]["name"],
                        p["backend"]["service"]["port"]["number"],
                    )
                )
        by_svc = {svc: (path, port) for path, svc, port in paths}
        assert "gateway" in by_svc and by_svc["gateway"][1] == 8080
        assert "auth" in by_svc and by_svc["auth"][1] == 8081
        assert "patient-flow" in by_svc and by_svc["patient-flow"][1] == 8082
        assert "cds" in by_svc and by_svc["cds"][1] == 8083
        # Path prefixes the SPA already uses.
        joined = " ".join(p for p, _, _ in paths)
        assert "/api" in joined and "/auth" in joined
        assert "/flow" in joined and "/cds" in joined

    def test_tls_is_required(self):
        docs = list(yaml.safe_load_all((K8S / "ingress.yaml").read_text()))
        tls = docs[0]["spec"].get("tls") or []
        assert tls, "Ingress must terminate TLS — PHI must not ride plaintext HTTP"


# ---------------------------------------------------------------------------
# 6. Mongo client configuration pins replica-set + retryable writes
# ---------------------------------------------------------------------------


class TestMongoClientProductionShape:
    def test_create_client_enables_retryable_writes(self):
        import inspect

        from backend.services.patient_flow.repository import create_client

        # Source-level pin: retryWrites=True must stay on the constructor.
        src = inspect.getsource(create_client)
        assert "retryWrites=True" in src
        assert "serverSelectionTimeoutMS" in src

    def test_configmap_mongo_uri_requests_replica_set(self):
        docs = list(yaml.safe_load_all((K8S / "config.yaml").read_text()))
        cm = next(d for d in docs if d and d.get("kind") == "ConfigMap")
        uri = cm["data"]["MONGO_URI"]
        low = uri.lower()
        assert "replicaset=" in low
        assert "retrywrites=true" in low

    def test_mongomock_does_not_support_multi_doc_transactions(self):
        """Honest residual: mongomock cannot prove transaction atomicity.

        This test locks the current limitation so a future real-mongod suite
        is clearly additive rather than a silent replacement.
        """
        pytest.importorskip("mongomock_motor")
        import asyncio

        from mongomock_motor import AsyncMongoMockClient

        async def attempt():
            client = AsyncMongoMockClient()
            # mongomock_motor may not expose start_session; either way a
            # multi-doc txn is not meaningful on the mock.
            start = getattr(client, "start_session", None)
            if start is None:
                return "no-start_session"
            try:
                async with client.start_session() as session:
                    async with session.start_transaction():
                        await client["t"]["a"].insert_one({"x": 1}, session=session)
                        await client["t"]["b"].insert_one({"x": 2}, session=session)
                return "txn-ok"
            except Exception as exc:
                return f"txn-unsupported:{type(exc).__name__}"

        result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(attempt())
        assert result != "txn-ok", (
            "mongomock unexpectedly ran a multi-doc transaction; "
            "update residual docs — real mongod coverage may now be redundant"
        )
