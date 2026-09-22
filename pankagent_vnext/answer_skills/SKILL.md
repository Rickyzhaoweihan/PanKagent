---
name: pankagent-bim-answer-skills
description: Maintain the pinned BIM interpretation bundle and deterministic schema routing used when PanKagent explains retrieved graph evidence.
---

# BIM answer interpretation bundle

This project-local bundle supplies scientific interpretation guidance for
PanKagent answers. [manifest.json](manifest.json) is the routing and provenance
contract. The user's request and the application's established answer format
remain authoritative; upstream response templates supply content guidance.

## Routing contract

- Match canonical labels and relationship types from returned graph evidence.
  Resolve only the explicit node and edge aliases in the manifest. A node's
  semicolon-separated labels form a set; generic `provenance` alone does not
  activate donor guidance. Relationship aliases select interpretation rules
  without rewriting queries or graph types.
  The release's exact `Sample_node` label maps to the source's `Sample node`
  rule. `ASSOCIATED_WITH_GO` shares ontology-annotation interpretation with
  `FUNCTION_ANNOTATION`.
- Every nonempty match condition must hold. `nodes_any` and `edges_any` each
  require at least one listed canonical type; `nodes_all` and `edges_all`
  require every listed type. `min_edge_types` counts distinct matched types
  from that rule's `edges_any` list, after alias canonicalization.
- Emit each selected scientific text once. The two RNA/ATAC predicates share
  one source entry and must deduplicate when both accessibility types occur.
  The preferred donor/provenance source also covers plain donor nodes.
- Use [schema guidance](bim/schema_skill.json) for matched node, edge, and
  composite rules. The manifest excludes obsolete DEG/expression/OCR
  assumptions; current OCR peaks retain peak-level semantics.
- Use [functional guidance](bim/functional_data_interpretation_skill.json)
  only for exact feature names present in the evidence. Preserve hormone,
  units, normalization, and the distinction between index and AUC measures.
  Unmatched features remain explicitly uninterpreted.
- Use [staging definitions](bim/general_interpretation.json) only for relevant
  clinical fields, with the application's clinical safeguards. These
  definitions do not independently establish a donor's diagnosis or resolve
  conflicting recorded metadata.
- Attach selected rule IDs, matched canonical types/features, source commit,
  and bundle version to answer provenance. Interpretation guidance is distinct
  from retrieved evidence and does not establish result completeness.

## Source and maintenance

Source: [RingoMao/PanKagent-BIM-skills, BIM_skill](https://github.com/RingoMao/PanKagent-BIM-skills/tree/40cb7f5b08a2082a4f67ae7198591d92fa0c175d/BIM_skill),
commit `40cb7f5b08a2082a4f67ae7198591d92fa0c175d`. No license file was supplied
at this commit; this bundle retains attribution and adds no license grant.

`upstream/` preserves all three supplied JSON files byte for byte. `bim/`
contains strict JSON with versioned application interpretation overrides; the pinned originals remain in upstream/. The schema file
required removal of one trailing comma outside quoted strings; its source
character offset and transformation are recorded in `manifest.normalization`.
The initial import of the other two files changed serialization formatting only.
Current `bim/` files also contain the application overrides recorded in the
manifest: in particular, active general guidance reports recorded clinical
metadata while the original staging definitions remain archived in `upstream/`.

When updating the source, pin the new commit, preserve originals, normalize
outside quoted strings, validate every rule reference, and recalculate all six
SHA-256 values. Check alias ambiguity and modern/legacy OCR separation. Run
router tests for unrelated evidence, multi-label nodes, composite predicates,
exact functional fields, clinical safeguards, and deterministic deduplication.

## Investigator-facing caveats

Maintain [common_caveats.md](bim/common_caveats.md) for evidence-conditioned caveat wording and one-versus-rest semantics. It is loaded once into the synthesis contract; no extra model call is made. After editing it or normalized schema guidance, bump bundle_version and update the corresponding SHA-256 in manifest.json. Keep upstream/ unchanged. Source-analysis comparison scope must not be confused with query scope or model-context sampling.

The backend stamps evidence_coverage after successful validated execution. Keep query scope, source-analysis comparator, model context and graph display separate. Interpret complete zero-match scopes as no matching PanKgraph records in that release; unknown legacy and partial/failed results never become exhaustive absence. Source one-versus-rest semantics must remain correct even for a restricted query. See the coverage and streaming scope-guard regression tests when updating this contract.

## Interpreting counts, clinical metadata and GO evidence

For donor/sample counts, return the requested aggregate and selection rules. Do not add individual clinical examples unless requested. Active staging guidance reports recorded metadata; historical clinical definitions remain preserved only in upstream/ and must not be treated as independently verified diagnostic criteria.

Use the glossary's formal GO expansions and categories. IBA means “Inferred from Biological aspect of Ancestor.” TAS is an author-statement code; it neither asserts a direct assay nor proves the absence of experiments. IEA describes automatic annotation, not the absence of scientific support. See the [official GO evidence guide](https://geneontology.org/docs/guide-go-evidence-codes/).

Model excerpts can omit properties or contain minimal endpoint records. Those choices never establish missing data in Neo4j. Preserve verified retrieval coverage and keep internal excerpt details out of scientific caveats.

For QTLs, distinguish indexed relationship count from the source credible-set size (n_snp). A single returned association is not a single-variant credible set. Tissue describes the association context; it does not make the variant tissue-specific. Use expression or splicing wording only when the molecular phenotype is recorded. A single PIP cannot rank or characterize the remaining variants. A source label such as exon does not establish splicing or an sQTL class. Different full credible_set IDs identify recorded sets; they do not establish independent signals or rule out shared biology. Preserve full IDs or an unambiguous display mapping, and never call a set unnamed when its ID is recorded.

For cell-type counts, say how many types have matching evidence. A complete query does not prove that the returned matches equal all cell types profiled in the source study. For fGSEA, explicitly identify an unavailable ranked contrast; a positive score alone cannot establish pathway activation. These distinctions belong beside the relevant measurement.

Use the versioned full-record answer facts before selected examples: lead roles, original assay counts and method/source distributions remain authoritative for the retrieved set. An excerpt does not create missing biological data. Formal GO expansions come from the recorded-code glossary.
