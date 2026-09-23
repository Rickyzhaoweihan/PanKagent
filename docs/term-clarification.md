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
