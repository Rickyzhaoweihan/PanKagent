# Immutable agent schema pack

The editable source of graph knowledge is `packs/pankgraph/` in PanKagent, not the
frontend. The metadata-only manifest pins the general `KG_agent/kg-standard`
v0.3.0 contract and references four modules:

1. **database_schema.json** — exact node/relationship/property names, directed
   endpoint signatures, identity fields, recorded synonyms, reviewed aliases,
   ownership, annotation links, export policy, and generated observations.
2. **query_patterns.json** — structural patterns, roles, connected retrieval and
   counting/combination guidance. No benchmark answers or hardcoded populations.
3. **semantic_interpretation.json** — dataset/type-specific meanings and all 87
   source entries from the three pinned BIM files. Historical representations,
   clinical stage explanations and multi-assay interpretation do not authorize
   filters on the current graph.
4. **validation.json** — registered checks, completeness, repair guidance and
   resource limits. JSON names tested operators; it never executes arbitrary code.

`contracts/` provides JSON Schema contracts; `SchemaPack` also checks references,
relationship endpoints, path patterns, rule coverage and bounded budgets.
`views.py` derives old consumer envelopes from this single source during migration.
These projections are read-only compatibility APIs, not additional configuration.
Complex biological parsing remains tested Python; this is not a general rule language.

## Reviewed definitions versus generated observations

Descriptions, patterns, units, aliases, topology expectations and interpretation
references are reviewed declarations. The `observations` objects describe a
particular release; they never become automatic query filters or numeric limits.
An unknown/unprofiled value is not forbidden. Declared aliases do not replace live
identity verification. A sample tissue code and a tissue-node display name are
separate property domains even when they describe related anatomy.

Each complete property profile reports its distinct non-null count, owning-record
frequencies and stored types. Domains of zero through four values are exhaustive;
larger domains contain the top three examples. Ties use canonical typed values.
Numeric strings stay strings. Numeric ranges are observed, not normative. Lists
retain whole-list examples and a separate element profile that counts each element
once per owner. Missing values, empty strings/lists and literal sentinel strings
remain distinct. Partial scans cannot claim exhaustive domains or counts.

Public types with fewer than five records include every public prototype;
otherwise three deterministic examples are retained. Protected donor/sample
records and their identifiers are never exported. Only approved aggregate property
domains may be profiled. Unknown fields require review before export.

## Read-only extraction

Run from an environment with the configured Neo4j read credentials:

```sh
python scripts/extract_database_schema.py \
  pankagent_vnext/agent_schemas/packs/pankgraph/database_schema.json \
  /private/review/database-candidate.json
```

The script uses READ sessions and ordinary Cypher, without APOC or a model. It
preserves reviewed declarations, emits the candidate plus a unified diff and
separate timestamped audit, and validates the artifact contract. Use `--types`
for a partial diagnostic extraction; it is explicitly not a complete release.
Unknown definitions and failed profiles are listed for review, never silently
classified as absent. Output is a candidate, not a live configuration update.
The extractor uses private file permissions and checks the entire export against
protected record IDs, including relationship annotations and endpoint examples.
It writes a value-free privacy audit and refuses the final export if a protected
identifier is found. Checkpoints and diffs must still stay in protected storage.

Keep full reference memberships and any restricted diagnostics in protected
storage. Git contains public schema facts, safe aggregates and synthetic fixtures.

## Runtime access and pinning

M03 and M04 use the same pack and verified identity evidence. M04 can inspect a
node/property definition with linked interpretation/patterns, inspect a retained
BIM rule using `interpretation.<rule-id>`, resolve entities and look up recorded
values for an explicit owner/property. All lookups share existing bounded limits.

`PANK_VNEXT_SCHEMA_PACK` selects a directory at startup only. The canonical digest
covers the manifest and all four modules, including observations; runs, caches,
identity proofs and session references pin it. Callers receive copies. Deploy the
code and pack together; active investigations never hot reload file edits.
PostgreSQL OCR mappings remain disabled until release/coordinate/membership checks
pass. Neo4j questions do not depend on PostgreSQL readiness.

## Acceptance

`tests_vnext/fixtures/acceptance/frontend.json` records the refreshed frontend
catalog commit and digests with concrete parameter substitutions. HPAP references
must compare full typed memberships and counts, not counts alone. Tests preserve
stage versus diagnosis, exact versus RNA-capable assays, same-sample tissue links,
complete backend populations and independent partial successes.

Run correctness once and the fixed eight performance controls three times for
both versions with identical settings/concurrency. Save cache state, preview/final
latencies, settled usage/cost and failure/repair counts. Only matched successful
controls enter regression comparisons. Newly supported questions are reported
separately. Failed baselines are never reclassified as acceptable partial answers.
Release requires the full acceptance report; offline tests or health alone do not
constitute live acceptance.

`examples/database_schema.synthetic.json` is a populated contract prototype with
synthetic gene, disease and relationship records. It is not a second schema pack
or evidence for a biological answer. The production dictionary's public examples
come from the pinned graph scan; clinical records are not used as Git examples.

## Temporary interpretation comments

`semantic_interpretation.json.temporary_comments` is a reviewed, release-scoped
bridge for meanings that are not yet explicit in KG properties. Each comment
names its section, relationships, graph release, agent guidance and removal
condition. The same text reaches grounded/fallback planning, relationship
interpretation and answer generation; it is included in the immutable pack hash.

The PanKgraph_08_04 QTL/GWAS comment records the project convention that indexed
edges represent lead signals. Missing lead flags must not cause a blanket
"lead cannot be established" answer or an extra mandatory filter. Stored explicit
annotations remain unchanged, and coloc signal/lead identity must still be read
from the coloc record. S3 or future all-variant layers do not inherit this rule.
Replace this temporary comment when the next KG release directly encodes the
roles and the migration has been validated. This source update does not activate
a deployed service or rewrite existing answers.
