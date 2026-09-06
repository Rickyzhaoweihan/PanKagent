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
contains strict JSON with identical parsed biological content. The schema file
required removal of one trailing comma outside quoted strings; its source
character offset and transformation are recorded in `manifest.normalization`.
The other two files only have serialization formatting changes.

When updating the source, pin the new commit, preserve originals, normalize
outside quoted strings, validate every rule reference, and recalculate all six
SHA-256 values. Check alias ambiguity and modern/legacy OCR separation. Run
router tests for unrelated evidence, multi-label nodes, composite predicates,
exact functional fields, clinical safeguards, and deterministic deduplication.
