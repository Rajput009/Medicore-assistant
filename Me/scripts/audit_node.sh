#!/usr/bin/env bash
# Fail CI on known vulnerabilities in shipped npm packages.
#
# Two deliberate choices:
#
#   1. Only *runtime* dependencies gate the build (`--omit=dev`). Vite,
#      Vitest, esbuild and friends never reach a browser; a dev-server path
#      traversal is a real bug but it is not in the product, and treating it
#      as release-blocking trains people to ignore the gate.
#   2. Dev-dependency advisories are still printed, so they are visible and
#      can be upgraded on their own schedule.
#
# Exceptions are listed with a reason and a withdrawal condition. Anything not
# listed fails the build.
set -euo pipefail
cd "$(dirname "$0")/../frontend/web"

# --- Documented exceptions -------------------------------------------------
#
# GHSA-qwww-vcr4-c8h2 — react-router: RSC-mode CSRF bypass allows an action
#   to execute before a 400 response.
#   Status:   advisory range is 7.12.0–8.2.0, fixed in >=8.3.0. No 8.x has
#             been released, so there is nothing to upgrade to; we already
#             run the newest published 7.x.
#   Why safe: the vulnerability is in React Router's RSC/data-router action
#             pipeline. This SPA uses declarative routing only —
#             BrowserRouter/Routes/Route plus navigation hooks — with no
#             createBrowserRouter, no route `action`/`loader`, and no RSC.
#             The vulnerable code path is never constructed. Guarded by
#             src/routerSurface.test.tsx, which fails if the app starts using
#             a data router.
#   Re-check: withdraw as soon as a fixed release exists, or immediately if
#             that test fails.
IGNORE_ADVISORIES=(
  "GHSA-qwww-vcr4-c8h2"
)

echo "== Dev-dependency advisories (informational, non-blocking) =="
npm audit --audit-level=high || true

echo
echo "== Runtime dependency audit (blocking) =="
report="$(npm audit --omit=dev --json 2>/dev/null || true)"

python3 - "$report" "${IGNORE_ADVISORIES[@]}" <<'PY'
import json, sys

report_raw = sys.argv[1]
ignored = set(sys.argv[2:])

try:
    report = json.loads(report_raw) if report_raw.strip() else {}
except json.JSONDecodeError:
    print("could not parse npm audit output; failing closed")
    sys.exit(1)

blocking = []
for name, vuln in (report.get("vulnerabilities") or {}).items():
    if vuln.get("severity") not in ("high", "critical"):
        continue
    advisories = {
        via.get("url", "").rsplit("/", 1)[-1]
        for via in vuln.get("via", [])
        if isinstance(via, dict)
    }
    unexplained = advisories - ignored
    # A package whose advisories are all accounted for is not blocking. A
    # package that only appears because a *dependency* of it is vulnerable
    # carries no advisories of its own; it is covered by the dependency.
    if advisories and not unexplained:
        continue
    if not advisories:
        continue
    blocking.append((name, vuln.get("severity"), sorted(unexplained)))

if blocking:
    print("Blocking runtime vulnerabilities:")
    for name, severity, ids in blocking:
        print(f"  - {name} ({severity}): {', '.join(ids)}")
    print("\nUpgrade, or add a documented exception to scripts/audit_node.sh.")
    sys.exit(1)

print(f"No blocking runtime vulnerabilities ({len(ignored)} documented exception(s)).")
PY
