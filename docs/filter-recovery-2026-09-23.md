# Partial answers after filter rejection

Implementation commit: d6eee7b. Activated only on dev agent 8794 in
`20260923-filter-recovery-d6eee7b`; results and health owners unchanged.

The planner now retains a filter-rejected branch as an unavailable check instead
of blocking all independent checks. No predicates are removed or newly authorized.
A partial answer requires a verified, meaningful primary result after all checks
finish. Failed dependencies cannot supply IDs to a downstream query. Ambiguous
entities, unknown relations, and requests with no usable independent primary
branch retain the existing recovery behavior.

Warnings identify the rejected property/operator/value and explain the conclusion
that remains unresolved. Lead-variant filters explicitly distinguish naming a
variant from establishing its lead role. Existing evidence scope/title fields
carry these limitations through the unchanged formatter input/output code.

Validation: 64 focused recovery/readiness/independent-evidence tests, 181 query
and scope tests, and 45 synthesis/composable tests passed (overlapping selections).
An additional rendering regression verifies that the existing mandatory scope
fact retains the warning even when the model selects no facts.

Read-only replay of saved run 4e6104ad-6455-454d-8018-ae1cdaa02826 through the
candidate and then deployed preparation path returned two complete membership
checks (QTL and GWAS, each two nodes and one edge) and one failed colocalization
check. Readiness was partial, not full coverage. These membership records alone
do not establish the requested colocalization. No paid external model calls were
made for this change. Browser rendering and a new model-generated final answer
were not replayed. Private replay, validation, deployment and backup records are
under `/db/pankagent-vnext-private/operations/filter-recovery/`.
