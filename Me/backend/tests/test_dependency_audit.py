"""Guards for documented dependency-audit exceptions.

``scripts/audit_python.sh`` suppresses specific advisory IDs so CI can stay
green on a vulnerability we have shown is unreachable. That suppression is
only defensible while the reasoning holds, and reasoning rots silently — a
dependency upgrade or a packaging change can quietly make the vulnerable code
live again, and nobody would notice because the advisory is ignored.

These tests are the tripwire. If one fails, the corresponding exception in
audit_python.sh must be withdrawn, not the test relaxed.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = ROOT / "scripts" / "audit_python.sh"


class TestEcdsaExceptionRemainsValid:
    """PYSEC-2026-1325 — Minerva timing attack in pure-Python ``ecdsa``.

    We accept it because ``python-jose[cryptography]`` performs all EC work in
    ``cryptography`` (OpenSSL) and never calls into ``ecdsa``. ``ecdsa`` is
    merely a transitive dependency that gets installed and never executed.
    """

    def test_ec_keys_resolve_to_the_cryptography_backend(self):
        from jose.backends import ECKey
        from jose.backends.cryptography_backend import CryptographyECKey

        assert ECKey is CryptographyECKey, (
            "python-jose is no longer using the cryptography EC backend, so the "
            "vulnerable pure-Python ecdsa path may now be reachable. Withdraw "
            "the PYSEC-2026-1325 exception in scripts/audit_python.sh."
        )

    def test_es256_round_trip_never_touches_the_ecdsa_module(self):
        """The strong form of the claim.

        Runs in a subprocess with ``ecdsa`` replaced by a module that raises on
        *any* attribute access, then performs a real ES256 sign + verify. If
        the vulnerable library is used at all, this fails.
        """
        program = textwrap.dedent(
            """
            import sys, types

            class Tripwire(types.ModuleType):
                def __getattr__(self, name):
                    raise AssertionError(f"ecdsa.{name} accessed")

            for mod in ("ecdsa", "ecdsa.ecdsa", "ecdsa.keys", "ecdsa.curves"):
                sys.modules[mod] = Tripwire(mod)

            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from jose import jwt

            key = ec.generate_private_key(ec.SECP256R1())
            priv = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
            pub = key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode()

            token = jwt.encode({"sub": "audit-probe"}, priv, algorithm="ES256")
            assert jwt.decode(token, pub, algorithms=["ES256"])["sub"] == "audit-probe"
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0 and "OK" in result.stdout, (
            "An ES256 round trip touched the ecdsa module, so PYSEC-2026-1325 is "
            f"reachable and its exception must be withdrawn.\n{result.stderr[-2000:]}"
        )


class TestAuditScriptIsHonest:
    """The suppression list must stay small, deliberate and explained."""

    @pytest.fixture()
    def script(self) -> str:
        return AUDIT_SCRIPT.read_text()

    def test_the_script_exists_and_is_wired_into_ci(self, script):
        ci = (ROOT.parent / ".github" / "ci.yml.example").read_text()
        assert "audit_python.sh" in ci

    def test_every_ignored_id_is_documented(self, script):
        """An ID suppressed without a written reason is an undocumented risk
        acceptance, which is the failure mode this whole file exists to stop.
        """
        block = script.split("IGNORE_IDS=(", 1)[1].split(")", 1)[0]
        ignored = re.findall(r'"([A-Z]+-\d{4}-\d+)"', block)
        assert ignored, "expected at least one documented exception"
        for advisory in ignored:
            assert advisory in script.split("IGNORE_IDS=(")[0], (
                f"{advisory} is suppressed but not explained above the list"
            )
            assert "Why safe:" in script and "Re-check:" in script

    def test_the_exception_list_has_not_quietly_grown(self, script):
        """A deliberate speed bump: adding an exception should require editing
        this expectation, which forces the reviewer to look at the reasoning.
        """
        block = script.split("IGNORE_IDS=(", 1)[1].split(")", 1)[0]
        ignored = re.findall(r'"([A-Z]+-\d{4}-\d+)"', block)
        assert ignored == ["PYSEC-2026-1325"], (
            "The pip-audit suppression list changed. Confirm each entry is "
            "genuinely unreachable and guarded by a test in this file."
        )
