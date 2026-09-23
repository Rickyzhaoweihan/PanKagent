# Grounded clarification and bounded planning repair

Term suggestions run after existing grounding and before the planning-model call.
They consume only the complete, current source/tissue vocabulary. Candidate names
and alias IDs must exist in that inventory. Case variants and nearby spellings in
source/tissue positions produce an existing `recovery.suggestions` payload, with
the original term, match basis, full corrected question and exact recorded name.
Nothing is silently applied. Digits, stage numbers and explicit gene/variant
positions are excluded from fuzzy correction. Unknown acronyms in a scope position
produce a targeted clarification when there is no supported candidate.

PKN is not registered as a new alias. In a tissue position, it can be proposed as
a spelling candidate for the recorded PLN alias. The current pancreaticosplenic
lymph-node proxy qualifier remains visible. Multiple unique corrections are
bundled into one complete proposed question, preserving all untouched text.
Ambiguous single terms expose up to two alternatives; multiple ambiguities ask for
one combined clarification rather than guessing a combination.

The current frontend already renders selectable labeled suggestions and submits
their instructions through the existing revision interpreter. Suggested complete
questions appear in the explanation and instruction. No frontend rebuild or new
revision workflow is required. Normal revise/confirm/cancel behavior remains.

A verified source's exact registered parenthetical expansion is masked only for
tissue interpretation. In particular HPAP (Human Pancreas Analysis Program) does
not authorize a pancreas constraint; an independently requested pancreas tissue
remains meaningful. Independent donor-only plans whose only issues are generated
unrequested tissue filters receive one deterministic repair and ordinary full
revalidation. Chains, sample joins, requested ambiguities and other failures do
not use this repair shortcut. Remaining planner-added tissue failures explain the
planning error and offer the original full scope instead of blaming the user.

Formatting inputs, answer generation, graph-viewer evidence, source data and
clinical filter semantics are unchanged. No source predicate or stage relaxation
is introduced by a suggested spelling correction.

## Acceptance — 2026-09-23

314 focused tests and 38 subtests passed. Coverage includes scope preservation,
ambiguity, recorded proxy labels, original-question retention, no inference or
query execution during clarification, and unchanged formatter/viewer modules.

Live planning API checks returned structured `term_clarification` recovery for
both `hPAP` and tissue-position `PKN`, with the complete proposed questions and
zero planning-model calls. These use the existing failed-plan/revise lifecycle;
they do not create a new execution state. The existing dialog supports this
payload; rendered browser interaction was not revalidated.

The compact entity index does not include tissue names. A separate bounded,
release-verified terminology read supplies clarification candidates, including
when the larger index is unavailable. This does not replace compact grounding.

Implementation commit `623d27f` is deployed on agent 8794. Results 8795, health,
frontend and production were not replaced by this change. Private backup,
deployment and live acceptance evidence is under
`/db/pankagent-vnext-private/operations/term-clarification/`.
No paid external validation calls were required.

## Failed-dialog revision round trip

The legacy frontend retries terminal plans by submitting the previous question
plus `Requested change`. The backend now binds that exact same-session payload
to the saved failed clarification. Selecting an unchanged stored term suggestion
uses its complete recommended question directly, with the submitted text retained
in audit. A custom instruction uses the existing revision interpreter; its
standalone question is persisted before planning.

Historical repeated-correction tails are removed only when every tail repeats
the exact same accepted question. Different instructions are never discarded.
Regression tests exercise the real create -> failed clarification -> apply ->
new planning API sequence, including context-off sessions and manual edits.

Live round-trip acceptance for implementation `8fa04ab`: the three-repeat nPAP
payload was accepted, the next run persisted exactly `How many T1D stage 2 donors
are available in HPAP?`, and reached ordinary `awaiting_confirmation` with no
clarification and no repeated revision text. The 83 targeted regression tests
passed. Validation plans were closed; existing user history was not rewritten.
