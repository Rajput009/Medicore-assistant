#!/usr/bin/env bash
# Fail CI on known vulnerabilities in installed Python packages.
#
# Exceptions are listed explicitly below with a reason and a re-check date, so
# an accepted risk cannot quietly become a forgotten one. Anything not listed
# fails the build.
set -euo pipefail
cd "$(dirname "$0")/.."

# Audit the interpreter that actually runs the app. Falling back to a bare
# `python` can audit a different environment than the one under test, which
# would report a clean bill of health for packages nobody is running.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="$(command -v python3 || command -v python)"; fi
fi

"$PY" -m pip install -q pip-audit

# --- Documented exceptions -------------------------------------------------
#
# PYSEC-2026-1325 — ecdsa 0.19.2, Minerva timing attack on P-256.
#   Status:   no fixed version exists upstream (pure-Python ecdsa cannot
#             mitigate a timing side channel; maintainers say so explicitly).
#   Why safe: ecdsa is a transitive dependency of python-jose that we never
#             execute. We install python-jose[cryptography], which resolves
#             ECKey to CryptographyECKey; ES256 sign/verify runs entirely in
#             `cryptography` (OpenSSL). This is proven by a test that makes
#             any attribute access on the `ecdsa` module raise and then
#             performs a full ES256 round trip — see
#             backend/tests/test_dependency_audit.py.
#   Re-check: if that test ever fails, the vulnerable path has become
#             reachable and this exception must be withdrawn immediately.
IGNORE_IDS=(
  "PYSEC-2026-1325"
)

args=(--progress-spinner=off)
for id in "${IGNORE_IDS[@]}"; do
  args+=(--ignore-vuln "$id")
done

echo "pip-audit: ignoring ${#IGNORE_IDS[@]} documented exception(s): ${IGNORE_IDS[*]}"
exec "$PY" -m pip_audit "${args[@]}"
