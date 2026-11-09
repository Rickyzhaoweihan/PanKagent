# Restore dev literature retrieval — 2026-11-09

Both native entrypoints constructed `Workflow` with `literature=None`. New runs
therefore defaulted to graph-only, and even an explicit request could not invoke
the disconnected adapter. Saved CFTR runs confirmed this state. Reconnect the
existing `simple_agent.features.literature` adapter in `candidate_api.py` and
`query_release.py`; retain source-specific results, references and failure states.
The current release health now reports `literature_sources: [hirn, glkb]`.
No frontend changes or changes to literature selection prompts were required.

## Validation and deployment

- Three entrypoint/workflow regression tests verify default inclusion through
  confirmation, explicit opt-out, persistence/reload, and partial source failure
  without discarding a successful sibling. All 45 schema/workflow tests passed
  in the existing server runtime.
- Direct adapter test used only a public scientific CFTR question with no saved
  graph answer/history. GLKB returned four references; HIRN returned unavailable.
- A fresh public CFTR question traversed the deployed planning, confirmation,
  enrichment, formatting and literature stages. GLKB returned an answer with two
  references. HIRN was explicitly unavailable. Final status was correctly partial;
  one normalized literature perspective was retained. The final result projection
  contained the same literature sources as the saved run.
- Acceptance run: `simple-qwen-f4f60a4366f046bd83d1f5a76e5d998c`, session
  `e49aa96c066b4465981d461c9f33ef85`.
- Replaced only the two verified entrypoint files and restarted only idle dev
  port 18864. SHA-256 comparisons confirmed all 271 existing saved JSON files
  remained unchanged across deployment. No saved run was retrospectively enriched.
- Private deployment manifest, source backups, acceptance evidence and paired
  rollback live under
  `/db/pankagent-vnext-private/operations/literature-repair-20261109/`.
  Rollback is `rollback/restore.py`; it restores only the paired entrypoint files
  and owned dev process, preserving session/evidence/budget state.
- Deployed SHA-256: `candidate_api.py`
  `187836643f74e4835548b0dda5e92ad6982e14cb8d95a0fb979bcfadc2f2158a`;
  `query_release.py`
  `dc038ec8d3ff16fefe6deb7def559a246cd6b1306298758b31a47d0335420743`.
- Public browser visual inspection was blocked by site authentication
  (`ERR_INVALID_AUTH_CREDENTIALS`); API/run/result delivery was verified. This
  record does not claim visual UI acceptance.

## Outstanding HIRN dependency

Configured HIRN endpoint 8101 reports healthy and a library of 1,272 documents,
but a direct public-question stream returns a generation error: upstream HTTP
401, `invalid_api_key`. The coordinator exposes this as `upstream_error` while
preserving GLKB results. This is a second upstream failure, separate from the
disconnected workflow. HIRN requires its operator to replace its generation
credential and verify a fresh answer. No credential was copied between services,
no alternate corpus was substituted, and no shared HIRN service was restarted.
Reconnecting the adapter alone does not establish successful HIRN retrieval.
