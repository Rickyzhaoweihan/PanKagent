# Agent schema pack

The six JSON modules in `packs/pankgraph` are backend configuration, not frontend
visualization schemas. `manifest.json` pins the general `KG_agent/kg-standard`
v0.3.0 contract. The context-aware branch is not used.

1. Manifest: pack identity, module references and capabilities.
2. Graph/storage: physical types, directed endpoints, property ownership and optional stores.
3. Identity: canonical fields, public lookup labels, synonyms and index configuration.
4. Semantics/modalities: reviewed prompt modules and clinical/assay distinctions.
5. Query patterns: structural composition guidance; never benchmark answers or concrete filters.
6. Validation/repair: registered check order, bounded repair and completeness invariants.

`SchemaPack` validates cross-references and snapshots JSON at process startup.
`PANK_VNEXT_SCHEMA_PACK` selects a packaged directory at startup; questions cannot
select a path or hot reload a pack. The default is `packs/pankgraph`.
Consumers receive copies; active investigations cannot see edits made to files.
The canonical digest includes every module and belongs in run/cache/proof identity.
Deploy code and pack together. Never change credentials, endpoints or live data
through JSON. PostgreSQL mappings are explicitly unverified and disabled until
membership, coordinate conventions and release pairing pass independent checks.

To add a modality, edit module 4 and its structural pattern references in module 5,
then add a semantic regression fixture. To add a relationship, declare endpoints
and properties in module 2 before adding its interpretation/pattern. Generic
binding and execution safeguards cannot be bypassed by prompt text. Complex
clinical parsing and signal matching remain tested backend operators during this
incremental extraction; this pack does not claim all historical Python is generic.

Frontend acceptance is frozen by `scripts/freeze_frontend_acceptance.py` using
`src/schema/landing_sample_questions.json` and `landing_page_schema.json` from
`wangyiqunumich/pank_frontend`, branch `xuteng/react`. The first fixture is pinned
to `38c8f454eaecf79cacd7ef42d6b109595798791a`: 16 display entries expand to 14
unique concrete questions. HPAP tests are separate and require live recorded
category resolution and exact ID membership comparisons, not hardcoded counts.

Formal contracts for all six modules are in `contracts/*.schema.json`. Runtime
validation additionally checks endpoints, pattern paths, module versions and
registered compiler/budget limits. Invalid packs fail release preparation rather
than presenting a question-level grounding error.

Schema 3 also declares referential populations such as “these donors.” The
supervisor receives a typed reference and count, never formatter-sampled IDs.
The server rechecks the originating session, graph release, pack digest,
completeness and backend result fingerprint before binding dependent queries.
Ambiguous or incomplete previous populations cannot become unrestricted inputs.

The allocation policy partitions reservations across independent tasks. A
completed parent's unused reservation is divided between its direct children;
children cannot spend the same bytes twice, and unrelated branches retain their
allocation. Exact counts still require exhausted database cursors.
