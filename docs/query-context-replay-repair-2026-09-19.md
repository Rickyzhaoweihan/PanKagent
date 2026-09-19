# Query context and historical replay repairs

This candidate starts from the active dev agent commit `3b050fb56714`.
It is an application-local implementation. It does not change the pinned
KG standard, shared databases, Cypher service, or production agent.

## Oversized answer input

The shared `ClaudeGateway.prepare_answer` boundary is used by the vNext
agent and conventional results synthesis. Existing normal-size evidence
preparation is preserved. When the normal compact evidence exceeds 75,000
UTF-8 bytes, only node `id`, `type`, `description`, and `source` fields enter
the answer view. The fully serialized question/evidence payload has a separate
100,000-byte check and uses the same fallback if its envelope exceeds that cap.

The fallback preserves step references and retrieval status. It selects node
examples deterministically across steps and types, bounds description/source
text, and reports omitted node/edge/row counts in the application profile.
Stable IDs and types are never shortened: an individually oversized identity
record is omitted whole. Source is restricted to the recorded source name,
version and URL fields. No relationship measurements, rows, derived answer
facts or other node properties are exposed in this mode.

The versioned application answer bundle instructs the formatter to tell the
user the query is too broad for a detailed answer and suggest a more specific
query. This exception overrides the normal no-suggested-search rule only for
the limited view. Interpretation guidance about omitted measurements is
suppressed; node descriptions cannot justify associations or quantitative
claims. Original query evidence and graph/display/download data are unchanged.

This is a limited-view fallback, not a full context-specific KG standard.
It does not raise retrieval limits or turn an invalid query into valid evidence.
Retrieval truncation and model-context omission remain distinct.

## Regression scope

Synthetic tests cover ordinary measurement forwarding, oversized mixed evidence,
non-ASCII byte accounting, the final JSON envelope, immutable evidence,
whole-ID omission, rare node types, failed/truncated step disclosure and pinned
prompt hashes. Saved historical evidence is replayed privately and is not
committed to this repository. ssGSEA execution is excluded from this repair's
acceptance scope as requested; no ssGSEA tool is added.

Initial focused validation: 74 tests and 58 subtests passed across
`test_evidence_context`, `test_oversized_answer_context`,
`test_answer_synthesis`, and `test_answer_router`.

Live deployment and post-change inference acceptance are recorded separately
after they occur; this implementation report does not claim either is complete.
