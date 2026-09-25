# Conditional oversized-result fallback and Claude scope advice

**Update:** subsequently deployed with explicit user approval; see [deployment verification](deployment.md). The candidate-stage findings below are retained as history.

## Runtime change

After a query returns, the formatter-input adapter measures the serialized evidence
against its byte budget (normally 75,000 bytes). Only overflow activates the new
100-example fallback. Smaller results retain existing behavior, including explicit
identity-only views with more than 100 lightweight identities.

The fallback bounds illustrative nodes (including endpoint stubs) and identity/path
examples to at most 100 per collection. Existing stricter row/property/byte limits
still apply. Whole path witnesses are retained or omitted; dangling edges are removed.
The backend result, retrieval completeness, filtering, combinations, verified counts,
and independent viewer evidence are unchanged. This is not a `LIMIT 100` database
query and does not claim that only 100 samples exist. It prevents oversized input
from repeatedly reaching synthesis; it does not fix database timeouts or guarantee
that answer generation can never time out. The earlier ND timeout/deadline fix is
separately documented in the previous report.

`result_size_fallback` survives scientific input preparation and explicitly tells
the LLM the measured size, byte budget, fallback trigger, 100-example limit, and
that omitted examples are not absent data. Selection/omission metadata and verified
backend summaries remain separate. Incomplete retrieval stays incomplete. Short
parallel measurement evidence is not downgraded by another branch's overflow.

## M04 / D02 versus Python

The supervisor records Claude's selected constraints before Python finalization.
A missing Python lexical authorization proof becomes non-blocking advice for that
recorded predicate. The decision is tied to the step and effective request; helper
additions, changed predicates and changed requests cannot inherit it. Verified owner
normalization may add metadata without erasing the selected predicate.

The binding audit labels this `claude_semantic_interpretation`, not a deterministic
wording match. Existing database identity, supported property/topology, read-only
query and dependency checks still apply. No model call or retry allowance was added.

## Verification

- Focused tests cover conditional overflow, LLM-visible metadata, complete versus
  incomplete evidence, unchanged full counts, short parallel measurements, whole
  path witnesses, supervisor-stamped scope and helper-added predicates.
- Full offline suite: 2,797 passed, 19 failed, 5 skipped; 231 subtests passed.
  The 19 failing test identifiers exactly match the earlier release-suite log.
  This is not a globally passing release gate. Two historical byte-freeze tests
  were updated to allow the explicitly requested input-adapter changes while
  retaining formatter-output AST checks, unchanged viewer/results files, and the
  previously accepted execution-runtime implementation.
- Tab 01 rendered and inspected. All component geometry remains identical to HEAD;
  tabs 02–04 remain byte-for-byte identical. Only affected text/tooltips changed.

## Bounded CFTR replay

Question: “For CFTR, does the T1D GWAS signal colocalize with a pancreas splicing QTL?”

The first replay exposed an additional owner-normalization variant and still
blocked QTL preparation. After correcting the predicate comparison, the second
replay executed all three graph tasks. The QTL query retained both CFTR and
`PART_OF_QTL_SIGNAL.tissue_name = Pancreas`; Python's missing wording proof was
recorded as non-blocking advice. It returned three QTL records. The coloc query
returned one CFTR–T1D record, whose QTL signal ID matches the recorded credible-set-2
QTL annotation. The returned QTL source is `splicing; GTEx`.

The answer completed (1,894 characters), and persisted SSE deltas reconstruct it
exactly. The harness overall status is partial because its literature service is
intentionally unavailable. Its generic membership/reference fields do not establish
CFTR reference acceptance: this replay has no independent membership oracle.

**Remaining quality defect:** the planner also retrieved disease-wide T1D GWAS
membership (1,608 edges), rather than binding to the CFTR coloc GWAS signal. The
answer identified the displayed unrelated locus as unrelated, but that extra
context is still a planning/signal-linking defect. Successful QTL execution is not
claimed as complete scientific acceptance of every supporting branch.

Replay costs: first $0.1894085; second $0.185258. Second replay: 3 Claude calls,
29.153 seconds to preview, 55.478 seconds end-to-end. Cumulative existing $10 ledger:
$7.105156 settled, $0.213457 still reserved, $2.681387 available. No reset.

## Delivery boundary

Changes are committed on the existing Ringo branch. No deployment, new branch,
production change, database write, or formatter output-generation change is included.
The broader correctness/performance gates and GWAS scope issue still prevent a
release-acceptance claim. Live trace artifacts stay in the service-owned operations
directory `schema-consolidation-20260924/scope-advice-v2-cftr-check`.

## Subsequent user direction

The user accepts broad related GWAS retrieval as useful exploration, and permits
related links for simple disease definitions. Broad retrieval is not itself a
release blocker. The planner now explicitly keeps direct answer evidence primary,
labels exploration as context, and avoids making a large donor inventory the sole
or dominant evidence for a definition. It must still distinguish another locus
from the requested CFTR signal. The user authorized deployment of this candidate
to the single active dev agent; deployment evidence is recorded separately.
