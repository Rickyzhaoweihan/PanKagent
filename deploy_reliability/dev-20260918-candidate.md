# Integrated dev agent and health candidate

The Git parent is e4e24eaf603c194d9f5d6abda7d8a5310285fd63. Runtime and management/test directories were reconstructed from the actual deployed results source release 20260914-6c45e774; all 279 backend manifest hashes matched before edits. The entire active health package from 20260916-cypher33917 supersedes the older embedded package; its nine runtime hashes matched. Other tracked repository sources remain as they were at the Git parent.

This commit intentionally captures previously uncommitted but deployed source. The exact inherited bytes and reviewed overlay hashes are recorded in dev-20260918-provenance.json, distinguishing them from new work. All eight access/health overlay files and five metadata gate files passed baseline and candidate hash verification before integration. The three modified metadata base files matched the active results source too.

New behavior is limited to: authenticated Basic bootstrap and access status; an exact protected dev Origin opt-in with cross-site writes still denied; an operator-only health alias under the existing public prefix; a separate credential-free fixed dev HTML/main bundle observation; and fail-closed donor BMI/ambiguous sex metadata handling. See the source tests and the separate deployment plan for browser behavior and rollout gates.

Protected results configuration must explicitly set PANK_RESULTS_TRUSTED_BROWSER_ORIGIN=https://dev.pankgraph.org for the dev reverse proxy. Its default remains empty, and all other nonempty values are refused. No CORS access or anonymous credential injection is added. Canonical /pankgraph/health remains intact. Dev must redirect its no-slash health path before rewriting to /pankgraph-vnext/health-dashboard/. Bootstrap returns only /agent-vnext; the frontend restores its own pending route.

Combined offline acceptance: 396 tests and 41 subtests passed; the exact command is in dev-20260918-tests.json. Dashboard JavaScript syntax passed. No graph, provider, production state, credentials or live service was used or modified by these tests.

The deployment tar deliberately contains backend source, management scripts, tests, provenance and a file manifest only. It does not contain a virtualenv, frozen runtime, frontend, state, secrets, caches or dataset. The deployment owner must stage a new release, reuse the verified existing Linux runtime, copy the unchanged isolated frontend, preserve protected state and current budget, and run the combined tests on Linux before activation. Actual dev-origin Basic/POST/SSE/health/browser acceptance remains required. Do not treat the source commit or archive as deployed.

The donor guard is a temporary deployment boundary, not a metadata repair: unsupported BMI and sex-at-birth coverage requests fail with an explicit explanation and no matching-record conclusion; historical answers are not rewritten. No shared service or legacy entry point is replaced by this candidate.
