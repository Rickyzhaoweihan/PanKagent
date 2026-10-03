# Dev cohort bug-fix deployment — 2026-10-03

Deployed after the user's explicit “Deploy” instruction. Only the validated nine-file patch was applied to live base `53481a3`; fix source is `0a2e8c6`. The canonical branch's broader introduction changes remain inactive. This is a composite release, not a wholesale deployment of Ringo.

Release: `20261003-cohort-fixes-0a2e8c6/backend`, agent PID 2620852, loopback 8794. Model remains `claude-sonnet-5-5`; deployed schema is 2.2.1. The application/schema fingerprints match the isolated paid evaluation exactly; see the adjacent JSON.

The ownership-aware manager stopped only the old agent and started the new one. Readiness, Neo4j, Cypher, Claude, HIRN and GLKB component checks are healthy. Results PID 2726368 is unchanged. Frontend assets, health supervisor, production, runtime configuration, sessions and budget ledgers were not replaced or reset. The five-database online backup was verified before activation; it is not a cross-database quiescent snapshot.

Post-deployment read-only replays of the saved model plans, freshly prepared by the deployed code, returned 0 exact scRNA-seq samples and 13 snMultiomics samples. Both used the ordinary template, retained filters and completed without truncation. Model generation was explicitly disabled in this replay; no fresh paid model call was made after deployment. The exact same code had passed paid preview/confirmation/answer checks immediately before deployment.

Public `/agent-vnext`, results readiness, and an existing run through the public agent gateway returned HTTP 200. The gateway intentionally does not expose the agent's health endpoint; direct agent readiness was checked on loopback. Browser rendering was not re-tested because frontend assets were unchanged.

Recovery: use the ownership-aware manager to stop this agent and start the retained `20260930-simple-literature-53481a3/backend` agent. Restore code only; do not restore old session/budget databases. Private activation and verification evidence is under `/db/pankagent-vnext-private/operations/cohort-fixes-20261003/`.
