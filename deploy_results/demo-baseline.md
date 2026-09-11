# Accepted PanKagent demo baseline

The user accepted the current PanKagent workflow as the functional baseline on
2026-09-11. This records acceptance, not a claim that every possible question
or production deployment has been validated.

Backend branch: `codex/vnext-jieliu3-demo` (from `codex/grounded-planning`).
Frontend: independent `pank_frontend_vnext` repository, branch
`vnext-jieliu3-demo`, commit `6dcea1d`.

The baseline retains grounded planning, validated retrieval, colocalization
patterns, native graph controls, centered list layouts, small-graph PyGraphviz,
and functional plots in the main visual panel. Gene-context fixes `663e2c4`
and `92f22d9` prevent grammatical connectors from becoming false gene anchors.

The final saved-plot repair upgrades old functional plot assets through the
production `result_page=Yes` endpoint while preserving stored answers and
cohort filters. Requests share in-flight refreshes; upstream failures have a
60-second retry cooldown. No model call is introduced by this migration.

Validation: 141 local tests passed covering grounding, functional API parameters,
and saved-plot migration. Live grounding was separately checked against the
actual catalog for the PLEKHM1 failure before this wrap-up.

Deployment status: grounding fixes were activated on isolated agent 8794.
The final saved-plot migration is committed but not activated: the demo host
refused SSH during wrap-up. Do not treat this commit as live deployment.
Existing production and nginx configuration were not changed.
