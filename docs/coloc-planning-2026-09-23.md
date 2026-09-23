# Colocalization topology and exact signal linkage

Both grounded and timeout-fallback planning prompts now specify:

- `Gene -[SIGNAL_COLOC_WITH]-> disease` (ADCY3 to T1D), never gene to SNP.
- Variant-to-disease GWAS membership and variant-to-gene QTL membership are
  separate checks; primary coloc evidence survives an unavailable membership check.
- `gwas_signal_id` matches `credible_set_id` with the reviewed release-specific
  selected-signal suffix normalization.
- `qtl_signal_id` matches `credible_set` with the gene, source and tissue required
  by `coloc_dataset`. Shared endpoints alone do not establish signal identity.
- Mentioning an rsID does not authorize a lead-variant restriction.

The existing backend already implements these comparisons in `coloc_scope.py`
and carries supporting record references into `coloc_linkage` evidence. This is
comparison of retrieved records; it does not imply exhaustive retrieval of all
variants in every referenced signal.

A forced-grounding-timeout planning test exposed an additional disease alias
issue. Resolution now reuses the existing reviewed, release-scoped T1D alias for
signal queries, checks the canonical disease ID against the live graph, and
preserves the original literal for request authorization. Formatter code and
output prompts are unchanged.

Fresh paid fallback planning plus live read-only retrieval passed for the ADCY3 /
rs13393590 question: all three checks completed and both primary coloc records
linked to the requested GWAS signal. One also had verified corresponding QTL
membership; the other retained an explicitly unverified QTL membership. Three
validation planning attempts cost $0.072895 total, with no outstanding reservations
and a separate cumulative $1 validation cap within the user's $20 ceiling.

Implementation: c8890f9, af31f3e, 441027a; test runner correction 44267c3.
Private plans, exact evidence and cost records:
`/db/pankagent-vnext-private/operations/coloc-guidance/`.
No new browser-rendered final-answer validation was performed.
